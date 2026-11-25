from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    ProviderAdapter,
    load_bundled_catalog,
)
from cost_aware_model_router.metrics import render_prometheus_metrics
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    PolicyKind,
    PolicySpec,
    PricingCatalog,
    ProvenanceTag,
    TaskCategory,
    UsdDecimal,
    WorkloadItem,
    format_usd_str,
    quantize_usd,
)
from cost_aware_model_router.telemetry import (
    InMemorySpanExporter,
    OTelAttributeValue,
    OTelSpanRecord,
    generate_span_id,
    generate_trace_id,
)
from cost_aware_model_router.thermostat import (
    AgentBudgetThermostat,
    ThermostatAction,
    ThermostatDecision,
)


class ThermostatBlockedError(Exception):
    """Raised when AgentBudgetThermostat blocks a request (loop, budget cap, or quality floor)."""

    def __init__(self, decision: ThermostatDecision) -> None:
        super().__init__(decision.explanation)
        self.decision = decision


CODE_OR_SQL_PATTERN = re.compile(
    r"\b(SELECT|UPDATE|INSERT|DELETE|CREATE\s+INDEX|ALTER\s+TABLE|def\s+\w+|class\s+\w+|```)\b",
    re.IGNORECASE,
)
REASONING_PATTERN = re.compile(
    r"\b(prove|counterexample|invariant|quorum|step-by-step|formal|consensus)\b",
    re.IGNORECASE,
)


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "developer", "tool"] = "user"
    content: str = Field(min_length=1)


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible /v1/chat/completions request wire model with optional router metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(default="auto")
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    request_id: str | None = None
    category: TaskCategory | None = None
    x_org_id: str | None = None
    x_agent_id: str | None = None
    x_policy_id: str | None = None
    x_shadow_candidate_policy_id: str | None = None

    def combined_prompt(self) -> str:
        return "\n".join(m.content for m in self.messages)


class ChatCompletionChoiceMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = 0
    message: ChatCompletionChoiceMessage
    finish_reason: Literal["stop", "length", "error"] = "stop"


class ChatCompletionUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: UsdDecimal
    cost_provenance: str

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class RouterMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    org_id: str
    agent_id: str
    policy_id: str
    selected_tier: ModelTier
    routed_model: str
    latency_ms: float
    trace_id: str
    span_id: str
    shadow_evaluated: bool = False
    shadow_policy_id: str | None = None
    thermostat_action: str | None = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible /v1/chat/completions response wire model with router telemetry metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    router_metadata: RouterMetadata


class ShadowComparisonRecord(BaseModel):
    """Zero-risk shadow routing comparison record contrasting primary vs candidate policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    org_id: str
    agent_id: str
    primary_policy_id: str
    primary_tier: ModelTier
    primary_cost_usd: UsdDecimal
    primary_latency_ms: float
    shadow_policy_id: str
    shadow_tier: ModelTier
    shadow_cost_usd: UsdDecimal
    shadow_cost_delta_usd: UsdDecimal
    would_downgrade: bool
    explanation: str

    @field_validator("primary_cost_usd", "shadow_cost_usd", "shadow_cost_delta_usd", mode="before")
    @classmethod
    def _validate_costs(cls, v: Any) -> Decimal:
        return quantize_usd(v)


def _infer_workload_item(
    req: ChatCompletionRequest,
    *,
    default_org_id: str,
    default_agent_id: str,
    seq_num: int,
) -> WorkloadItem:
    prompt_text = req.combined_prompt()
    req_id = req.request_id or f"req-{seq_num:03d}"
    org_id = req.x_org_id or default_org_id
    agent_id = req.x_agent_id or default_agent_id

    has_code = bool(CODE_OR_SQL_PATTERN.search(prompt_text))
    multi_step = bool(REASONING_PATTERN.search(prompt_text))
    est_tokens = max(12, len(prompt_text.split()) * 4)

    if req.category is not None:
        cat = req.category
    elif multi_step:
        cat = TaskCategory.REASONING
    elif has_code:
        cat = TaskCategory.CODE_SQL
    elif "classify" in prompt_text.lower():
        cat = TaskCategory.CLASSIFICATION
    elif "summarize" in prompt_text.lower():
        cat = TaskCategory.SUMMARIZATION
    else:
        cat = TaskCategory.JSON_EXTRACTION

    if cat == TaskCategory.REASONING:
        features = ComplexityFeatures(
            prompt_tokens_est=max(est_tokens, 880),
            has_code_or_sql=True,
            schema_depth=3,
            multi_step_reasoning=True,
            domain_difficulty=0.90,
        )
    elif cat == TaskCategory.CODE_SQL:
        features = ComplexityFeatures(
            prompt_tokens_est=max(est_tokens, 600),
            has_code_or_sql=True,
            schema_depth=2,
            multi_step_reasoning=False,
            domain_difficulty=0.55,
        )
    elif cat == TaskCategory.SUMMARIZATION:
        features = ComplexityFeatures(
            prompt_tokens_est=max(est_tokens, 450),
            has_code_or_sql=False,
            schema_depth=2 if multi_step else 1,
            multi_step_reasoning=multi_step,
            domain_difficulty=0.40 if multi_step else 0.25,
        )
    elif cat == TaskCategory.CLASSIFICATION:
        features = ComplexityFeatures(
            prompt_tokens_est=est_tokens,
            has_code_or_sql=False,
            schema_depth=0,
            multi_step_reasoning=False,
            domain_difficulty=0.10,
        )
    else:
        features = ComplexityFeatures(
            prompt_tokens_est=est_tokens,
            has_code_or_sql=False,
            schema_depth=1,
            multi_step_reasoning=False,
            domain_difficulty=0.15,
        )

    return WorkloadItem(
        request_id=req_id,
        org_id=org_id,
        agent_id=agent_id,
        category=cat,
        prompt=prompt_text,
        features=features,
        evaluators=[EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=5000.0)],
    )


def _select_tier_for_policy(item: WorkloadItem, policy: PolicySpec) -> tuple[ModelTier, str]:
    """Select the target ModelTier and explanation for online gateway routing."""
    if policy.kind == PolicyKind.STATIC_TIER:
        target = policy.target_tier or ModelTier.FRONTIER
        return target, f"Static policy '{policy.policy_id}' selected {target.value}."

    if policy.kind == PolicyKind.COMPLEXITY_THRESHOLD:
        c_max = policy.compact_max_score if policy.compact_max_score is not None else 0.30
        m_max = policy.mid_max_score if policy.mid_max_score is not None else 0.65
        score = item.features.weighted_score()
        if score < c_max:
            return (
                ModelTier.COMPACT,
                f"Complexity score {score:.2f} < {c_max:.2f} -> compact tier.",
            )
        if score < m_max:
            return (
                ModelTier.MID,
                f"Complexity score {score:.2f} in [{c_max:.2f}, {m_max:.2f}) -> mid tier.",
            )
        return (
            ModelTier.FRONTIER,
            f"Complexity score {score:.2f} >= {m_max:.2f} -> frontier tier.",
        )

    if policy.kind == PolicyKind.CASCADE_ESCALATION:
        score = item.features.weighted_score()
        if score < 0.30:
            return ModelTier.COMPACT, f"Cascade initial tier compact (score {score:.2f})."
        if score < 0.65:
            return ModelTier.MID, f"Cascade escalated to mid (score {score:.2f})."
        return ModelTier.FRONTIER, f"Cascade escalated to frontier (score {score:.2f})."

    return (
        ModelTier.FRONTIER,
        f"Non-deployable oracle policy '{policy.policy_id}' defaulted to frontier.",
    )


class OpenAIRouterGateway:
    """OpenAI-compatible router endpoint with OTel span exporter and zero-risk shadow recorder."""

    def __init__(
        self,
        *,
        adapter: ProviderAdapter | None = None,
        catalog: PricingCatalog | None = None,
        primary_policy: PolicySpec | None = None,
        shadow_policy: PolicySpec | None = None,
        policies: list[PolicySpec] | None = None,
        span_exporter: InMemorySpanExporter | None = None,
        thermostat: AgentBudgetThermostat | None = None,
        shadow_log_path: Path | None = None,
        default_org_id: str = "org_demo",
        default_agent_id: str = "agent_gateway",
    ) -> None:
        self._adapter: ProviderAdapter = adapter or DeterministicFixtureAdapter()
        self._catalog: PricingCatalog = catalog or load_bundled_catalog()
        suite = policies or default_policy_suite()
        self._policies_by_id: dict[str, PolicySpec] = {p.policy_id: p for p in suite}

        if primary_policy is not None:
            self._policies_by_id[primary_policy.policy_id] = primary_policy
            self._primary_policy = primary_policy
        else:
            self._primary_policy = self._policies_by_id.get("threshold-balanced", suite[0])

        if shadow_policy is not None:
            self._policies_by_id[shadow_policy.policy_id] = shadow_policy
        self._shadow_policy = shadow_policy

        self._span_exporter = span_exporter or InMemorySpanExporter()
        self._thermostat = thermostat
        self._shadow_log_path = shadow_log_path
        self._default_org_id = default_org_id
        self._default_agent_id = default_agent_id
        self._lock = threading.Lock()
        self._seq_counter = 0
        self._shadow_records: list[ShadowComparisonRecord] = []

    @property
    def span_exporter(self) -> InMemorySpanExporter:
        return self._span_exporter

    def get_shadow_records(self) -> list[ShadowComparisonRecord]:
        with self._lock:
            return list(self._shadow_records)

    def _resolve_policy(self, policy_id: str | None, fallback: PolicySpec) -> PolicySpec:
        if not policy_id:
            return fallback
        found = self._policies_by_id.get(policy_id)
        if found is None:
            raise ValueError(f"Unknown policy_id {policy_id!r}")
        if not found.deployable:
            raise ValueError(
                f"Policy {policy_id!r} is marked deployable=False and cannot route live requests"
            )
        return found

    async def handle_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        start_ns = time.time_ns()
        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter

        primary_spec = self._resolve_policy(request.x_policy_id, self._primary_policy)
        shadow_spec: PolicySpec | None = self._shadow_policy
        if request.x_shadow_candidate_policy_id:
            shadow_spec = self._resolve_policy(
                request.x_shadow_candidate_policy_id,
                self._shadow_policy or self._primary_policy,
            )

        workload_item = _infer_workload_item(
            request,
            default_org_id=self._default_org_id,
            default_agent_id=self._default_agent_id,
            seq_num=seq,
        )

        primary_tier, _ = _select_tier_for_policy(workload_item, primary_spec)
        effective_tier = primary_tier
        thermostat_action_val: str | None = None
        seed_key = f"{workload_item.request_id}:{seq}:{primary_spec.policy_id}"
        trace_id = generate_trace_id(seed_key)
        span_id = generate_span_id(seed_key)
        created_ts = int(start_ns // 1_000_000_000)
        completion_id = f"chatcmpl-{hashlib.sha256(seed_key.encode()).hexdigest()[:16]}"

        if self._thermostat is not None:
            thermostat_decision = self._thermostat.evaluate_request(
                request_id=workload_item.request_id,
                org_id=workload_item.org_id,
                agent_id=workload_item.agent_id,
                prompt=workload_item.prompt,
                requested_tier=primary_tier,
                features=workload_item.features,
            )
            thermostat_action_val = thermostat_decision.action.value
            if not thermostat_decision.allowed:
                raise ThermostatBlockedError(thermostat_decision)
            if (
                thermostat_decision.action == ThermostatAction.CACHE_HIT
                and thermostat_decision.cached_response is not None
            ):
                end_ns = max(start_ns + 1, time.time_ns())
                zero_cost = Decimal("0.00000000")
                model_spec = self._catalog.models[effective_tier]
                span = OTelSpanRecord.create_router_span(
                    system=model_spec.provider,
                    request_model="semantic-cache",
                    input_tokens=0,
                    output_tokens=0,
                    org_id=workload_item.org_id,
                    agent_id=workload_item.agent_id,
                    policy_id=primary_spec.policy_id,
                    selected_tier=effective_tier,
                    cost_usd=zero_cost,
                    cost_provenance=ProvenanceTag.MODELED,
                    shadow_mode=False,
                    start_time_unix_nano=start_ns,
                    end_time_unix_nano=end_ns,
                    trace_id=trace_id,
                    span_id=span_id,
                    extra_attributes={"cost_router.thermostat_action": thermostat_action_val},
                )
                self._span_exporter.record(span)
                self._thermostat.record_outcome(
                    request_id=workload_item.request_id,
                    org_id=workload_item.org_id,
                    agent_id=workload_item.agent_id,
                    category=workload_item.category,
                    tier=effective_tier,
                    provenance=ProvenanceTag.MODELED,
                    passed=True,
                    cost_usd=zero_cost,
                    input_tokens=0,
                    output_tokens=0,
                    cached=True,
                )
                return ChatCompletionResponse(
                    id=completion_id,
                    created=created_ts,
                    model="semantic-cache",
                    choices=[
                        ChatCompletionChoice(
                            index=0,
                            message=ChatCompletionChoiceMessage(
                                content=thermostat_decision.cached_response
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=ChatCompletionUsage(
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cost_usd=zero_cost,
                        cost_provenance=ProvenanceTag.MODELED.value,
                    ),
                    router_metadata=RouterMetadata(
                        request_id=workload_item.request_id,
                        org_id=workload_item.org_id,
                        agent_id=workload_item.agent_id,
                        policy_id=primary_spec.policy_id,
                        selected_tier=effective_tier,
                        routed_model="semantic-cache",
                        latency_ms=0.1,
                        trace_id=trace_id,
                        span_id=span_id,
                        shadow_evaluated=False,
                        shadow_policy_id=None,
                        thermostat_action=thermostat_action_val,
                    ),
                )
            if thermostat_decision.approved_tier is not None:
                effective_tier = thermostat_decision.approved_tier

        traces: dict[tuple[str, ModelTier], ExecutionTrace] = await self._adapter.execute_workload(
            [workload_item], self._catalog
        )
        primary_trace = traces[(workload_item.request_id, effective_tier)]
        end_ns = max(start_ns + int(primary_trace.latency_ms * 1_000_000), time.time_ns())

        if self._thermostat is not None:
            self._thermostat.record_outcome(
                request_id=workload_item.request_id,
                org_id=workload_item.org_id,
                agent_id=workload_item.agent_id,
                category=workload_item.category,
                tier=effective_tier,
                provenance=primary_trace.cost_provenance,
                passed=True,
                cost_usd=primary_trace.cost_usd,
                input_tokens=primary_trace.input_tokens,
                output_tokens=primary_trace.output_tokens,
                prompt=workload_item.prompt,
                response_text=primary_trace.response_text,
                cached=False,
            )

        shadow_evaluated = False
        shadow_policy_id: str | None = None
        if shadow_spec is not None:
            shadow_evaluated = True
            shadow_policy_id = shadow_spec.policy_id
            shadow_tier, shadow_reason = _select_tier_for_policy(workload_item, shadow_spec)
            shadow_trace = traces[(workload_item.request_id, shadow_tier)]
            delta_usd = quantize_usd(shadow_trace.cost_usd - primary_trace.cost_usd)
            would_downgrade = TIER_ORDER.index(shadow_tier) < TIER_ORDER.index(effective_tier)
            shadow_rec = ShadowComparisonRecord(
                request_id=workload_item.request_id,
                org_id=workload_item.org_id,
                agent_id=workload_item.agent_id,
                primary_policy_id=primary_spec.policy_id,
                primary_tier=effective_tier,
                primary_cost_usd=primary_trace.cost_usd,
                primary_latency_ms=primary_trace.latency_ms,
                shadow_policy_id=shadow_spec.policy_id,
                shadow_tier=shadow_tier,
                shadow_cost_usd=shadow_trace.cost_usd,
                shadow_cost_delta_usd=delta_usd,
                would_downgrade=would_downgrade,
                explanation=(
                    f"Shadow candidate '{shadow_spec.policy_id}' selected {shadow_tier.value} "
                    f"vs primary '{primary_spec.policy_id}' ({effective_tier.value}); "
                    f"cost delta {format_usd_str(delta_usd)} USD. {shadow_reason}"
                ),
            )
            with self._lock:
                self._shadow_records.append(shadow_rec)
                if self._shadow_log_path is not None:
                    self._shadow_log_path.parent.mkdir(parents=True, exist_ok=True)
                    line = json.dumps(
                        shadow_rec.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    with self._shadow_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")

        model_spec = self._catalog.models[effective_tier]
        extra_attrs: dict[str, OTelAttributeValue] | None = (
            {"cost_router.thermostat_action": thermostat_action_val}
            if thermostat_action_val is not None
            else None
        )

        span = OTelSpanRecord.create_router_span(
            system=model_spec.provider,
            request_model=primary_trace.model_id,
            input_tokens=primary_trace.input_tokens,
            output_tokens=primary_trace.output_tokens,
            org_id=workload_item.org_id,
            agent_id=workload_item.agent_id,
            policy_id=primary_spec.policy_id,
            selected_tier=effective_tier,
            cost_usd=primary_trace.cost_usd,
            cost_provenance=primary_trace.cost_provenance,
            shadow_mode=shadow_evaluated,
            start_time_unix_nano=start_ns,
            end_time_unix_nano=end_ns,
            trace_id=trace_id,
            span_id=span_id,
            extra_attributes=extra_attrs,
        )
        self._span_exporter.record(span)

        return ChatCompletionResponse(
            id=completion_id,
            created=created_ts,
            model=primary_trace.model_id,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatCompletionChoiceMessage(content=primary_trace.response_text),
                    finish_reason="stop",
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=primary_trace.input_tokens,
                completion_tokens=primary_trace.output_tokens,
                total_tokens=primary_trace.input_tokens + primary_trace.output_tokens,
                cost_usd=primary_trace.cost_usd,
                cost_provenance=primary_trace.cost_provenance.value,
            ),
            router_metadata=RouterMetadata(
                request_id=workload_item.request_id,
                org_id=workload_item.org_id,
                agent_id=workload_item.agent_id,
                policy_id=primary_spec.policy_id,
                selected_tier=effective_tier,
                routed_model=primary_trace.model_id,
                latency_ms=primary_trace.latency_ms,
                trace_id=trace_id,
                span_id=span_id,
                shadow_evaluated=shadow_evaluated,
                shadow_policy_id=shadow_policy_id,
                thermostat_action=thermostat_action_val,
            ),
        )

    def summarize_economics(self, org_id: str | None = None) -> dict[str, Any]:
        if self._thermostat is None:
            raise ValueError("Thermostat is not configured on this gateway")
        return self._thermostat.summarize_economics(org_id=org_id).model_dump(mode="json")

    def build_shadow_summary(self) -> dict[str, Any]:
        records = self.get_shadow_records()
        total_primary = quantize_usd(
            sum((r.primary_cost_usd for r in records), Decimal("0.00000000"))
        )
        total_shadow = quantize_usd(
            sum((r.shadow_cost_usd for r in records), Decimal("0.00000000"))
        )
        total_delta = quantize_usd(total_shadow - total_primary)
        downgrade_count = sum(1 for r in records if r.would_downgrade)
        return {
            "total_shadow_requests": len(records),
            "downgrade_count": downgrade_count,
            "primary_total_cost_usd": format_usd_str(total_primary),
            "shadow_total_cost_usd": format_usd_str(total_shadow),
            "shadow_cost_delta_usd": format_usd_str(total_delta),
            "records": [r.model_dump(mode="json") for r in records],
        }

    def as_httpx_transport(self) -> httpx.MockTransport:
        return create_asgi_or_httpx_app(self)


def create_asgi_or_httpx_app(gateway: OpenAIRouterGateway) -> httpx.MockTransport:
    """
    Create an in-process httpx.MockTransport handler for POST /v1/chat/completions
    and GET /v1/finops/shadow-summary with zero external socket dependencies.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()

        if method == "POST" and path == "/v1/chat/completions":
            try:
                raw_body = json.loads(request.content.decode("utf-8"))
                if not isinstance(raw_body, dict):
                    return httpx.Response(
                        status_code=400,
                        json={"error": {"message": "Request body must be a JSON object"}},
                    )
                header_map = {
                    "x-org-id": "x_org_id",
                    "x-agent-id": "x_agent_id",
                    "x-policy-id": "x_policy_id",
                    "x-shadow-candidate-policy-id": "x_shadow_candidate_policy_id",
                }
                for h_name, field_name in header_map.items():
                    h_val = request.headers.get(h_name)
                    if h_val and field_name not in raw_body:
                        raw_body[field_name] = h_val

                chat_req = ChatCompletionRequest.model_validate(raw_body)
                chat_resp = await gateway.handle_chat_completion(chat_req)
                return httpx.Response(
                    status_code=200,
                    json=chat_resp.model_dump(mode="json"),
                )
            except ThermostatBlockedError as exc:
                return httpx.Response(
                    status_code=429,
                    json={
                        "error": {
                            "type": exc.decision.action.value,
                            "message": exc.decision.explanation,
                            "org_id": exc.decision.org_id,
                            "agent_id": exc.decision.agent_id,
                        }
                    },
                )
            except (ValueError, KeyError, TypeError) as exc:
                return httpx.Response(
                    status_code=400,
                    json={"error": {"message": str(exc)}},
                )

        if method == "GET" and path == "/v1/finops/shadow-summary":
            return httpx.Response(
                status_code=200,
                json=gateway.build_shadow_summary(),
            )

        if method == "GET" and path == "/v1/finops/economics":
            try:
                org_id = request.url.params.get("org_id")
                return httpx.Response(
                    status_code=200,
                    json=gateway.summarize_economics(org_id=org_id),
                )
            except ValueError as exc:
                return httpx.Response(
                    status_code=400,
                    json={"error": {"message": str(exc)}},
                )

        if method == "GET" and path == "/metrics":
            spans = gateway.span_exporter.get_spans()
            summary = (
                gateway._thermostat.summarize_economics()
                if gateway._thermostat is not None
                else None
            )
            payload = render_prometheus_metrics(spans, summary=summary)
            return httpx.Response(
                status_code=200,
                content=payload.encode("utf-8"),
                headers={"content-type": "text/plain; version=0.0.4; charset=utf-8"},
            )

        return httpx.Response(
            status_code=404,
            json={"error": {"message": f"Unsupported route {method} {path}"}},
        )

    return httpx.MockTransport(_handler)
