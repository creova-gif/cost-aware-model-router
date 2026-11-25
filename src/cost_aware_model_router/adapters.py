from __future__ import annotations

import asyncio
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ExecutionTrace,
    ModelTier,
    PricingCatalog,
    ProvenanceTag,
    TaskCategory,
    TierModelSpec,
    WorkloadItem,
    quantize_usd,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_bundled_catalog() -> PricingCatalog:
    catalog_path = FIXTURES_DIR / "catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    return PricingCatalog.model_validate(raw)


def generate_bundled_workload() -> list[WorkloadItem]:
    """Generate the deterministic 60-prompt Milestone 1 workload across 5 task families."""
    items: list[WorkloadItem] = []

    # 1. JSON Extraction (15 items, req-001..req-015): low complexity; all 3 tiers pass
    for idx in range(1, 16):
        req_id = f"req-{idx:03d}"
        items.append(
            WorkloadItem(
                request_id=req_id,
                org_id="org_demo",
                agent_id="invoice_extractor",
                category=TaskCategory.JSON_EXTRACTION,
                prompt=f"Extract customer_id, invoice_total, and currency as JSON for ticket #{idx}.",
                features=ComplexityFeatures(
                    prompt_tokens_est=120 + idx * 4,
                    has_code_or_sql=False,
                    schema_depth=1,
                    multi_step_reasoning=False,
                    domain_difficulty=0.15,
                ),
                evaluators=[
                    EvalRule(
                        eval_type=EvalType.JSON_SCHEMA,
                        schema_fields={
                            "customer_id": "str",
                            "invoice_total": "number",
                            "currency": "str",
                        },
                    ),
                    EvalRule(
                        eval_type=EvalType.JSON_FIELD_PREDICATE,
                        predicates={"currency": {"eq": "USD"}, "invoice_total": {"min": 10}},
                    ),
                    EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=4000.0),
                ],
                otel_attributes={"service.name": "billing-ingest", "workload.tier_hint": "compact"},
            )
        )

    # 2. Classification (15 items, req-016..req-030): low complexity; all 3 tiers pass
    for idx in range(16, 31):
        req_id = f"req-{idx:03d}"
        items.append(
            WorkloadItem(
                request_id=req_id,
                org_id="org_demo",
                agent_id="triage_router",
                category=TaskCategory.CLASSIFICATION,
                prompt=f"Classify support message #{idx} into BILLING, TECHNICAL, or ACCOUNT.",
                features=ComplexityFeatures(
                    prompt_tokens_est=95 + idx * 3,
                    has_code_or_sql=False,
                    schema_depth=0,
                    multi_step_reasoning=False,
                    domain_difficulty=0.10,
                ),
                evaluators=[
                    EvalRule(
                        eval_type=EvalType.EXACT_MATCH,
                        allowed_values=["BILLING", "TECHNICAL", "ACCOUNT"],
                    ),
                    EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=3500.0),
                ],
                otel_attributes={"service.name": "support-triage"},
            )
        )

    # 3. Summarization (10 items, req-031..req-040): medium complexity
    #    req-031..req-037 pass on Compact/Mid/Frontier; req-038..req-040 require Mid or Frontier
    for idx in range(31, 41):
        req_id = f"req-{idx:03d}"
        harder_summary = idx >= 38
        items.append(
            WorkloadItem(
                request_id=req_id,
                org_id="org_demo",
                agent_id="exec_briefing",
                category=TaskCategory.SUMMARIZATION,
                prompt=f"Summarize incident postmortem #{idx} with root_cause and action_items JSON.",
                features=ComplexityFeatures(
                    prompt_tokens_est=420 + idx * 8,
                    has_code_or_sql=False,
                    schema_depth=2 if harder_summary else 1,
                    multi_step_reasoning=harder_summary,
                    domain_difficulty=0.45 if harder_summary else 0.25,
                ),
                evaluators=[
                    EvalRule(
                        eval_type=EvalType.JSON_SCHEMA,
                        schema_fields={"root_cause": "str", "action_items": "list"},
                    ),
                    EvalRule(
                        eval_type=EvalType.REGEX_MATCH,
                        pattern=r"mitigation|rollback|quorum",
                        case_sensitive=False,
                    ),
                    EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=4200.0),
                ],
                otel_attributes={"service.name": "incident-summarizer"},
            )
        )

    # 4. Code & SQL Debugging (11 items, req-041..req-051): mid-to-high complexity
    #    Compact fails all 11; Mid passes all 11; Frontier passes all 11
    for idx in range(41, 52):
        req_id = f"req-{idx:03d}"
        items.append(
            WorkloadItem(
                request_id=req_id,
                org_id="org_demo",
                agent_id="sql_migration_auditor",
                category=TaskCategory.CODE_SQL,
                prompt=f"Fix lock-contention bug in PostgreSQL migration #{idx} and return JSON patch.",
                features=ComplexityFeatures(
                    prompt_tokens_est=580 + idx * 6,
                    has_code_or_sql=True,
                    schema_depth=2,
                    multi_step_reasoning=False,
                    domain_difficulty=0.55,
                ),
                evaluators=[
                    EvalRule(
                        eval_type=EvalType.JSON_SCHEMA,
                        schema_fields={"sql_patch": "str", "lock_safe": "bool"},
                    ),
                    EvalRule(
                        eval_type=EvalType.JSON_FIELD_PREDICATE,
                        predicates={"lock_safe": {"eq": True}},
                    ),
                    EvalRule(
                        eval_type=EvalType.REGEX_MATCH,
                        pattern=r"CONCURRENTLY|SET lock_timeout",
                    ),
                    EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=4500.0),
                ],
                otel_attributes={"service.name": "db-migration-bot"},
            )
        )

    # 5. Multi-Step Reasoning (9 items, req-052..req-060): high complexity
    #    req-052..req-058 require Frontier (Compact & Mid fail);
    #    req-059..req-060 fail on all 3 tiers (exercises D2 ear-oracle all-fail fallback & 96.7% frontier ceiling)
    for idx in range(52, 61):
        req_id = f"req-{idx:03d}"
        items.append(
            WorkloadItem(
                request_id=req_id,
                org_id="org_demo",
                agent_id="distributed_invariant_prover",
                category=TaskCategory.REASONING,
                prompt=(
                    f"Prove or construct a counterexample trace for consensus safety case #{idx} "
                    "and emit structured proof JSON."
                ),
                features=ComplexityFeatures(
                    prompt_tokens_est=850 + idx * 10,
                    has_code_or_sql=True,
                    schema_depth=3,
                    multi_step_reasoning=True,
                    domain_difficulty=0.90,
                ),
                evaluators=[
                    EvalRule(
                        eval_type=EvalType.JSON_SCHEMA,
                        schema_fields={
                            "invariant_holds": "bool",
                            "proof_steps": "list",
                            "verified_quorum": "int",
                        },
                    ),
                    EvalRule(
                        eval_type=EvalType.JSON_FIELD_PREDICATE,
                        predicates={"verified_quorum": {"min": 3}},
                    ),
                    EvalRule(eval_type=EvalType.LATENCY_SLA, max_latency_ms=5000.0),
                ],
                otel_attributes={"service.name": "formal-verifier"},
            )
        )

    return items


class ProviderAdapter(Protocol):
    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]: ...


class DeterministicFixtureAdapter:
    """Zero-API-key deterministic 3-tier replay adapter."""

    def _simulate_response(self, item: WorkloadItem, tier: ModelTier) -> tuple[str, int, float]:
        idx = int(item.request_id.split("-")[1])
        base_out_tokens = 85 + (idx % 17) * 5

        if item.category == TaskCategory.JSON_EXTRACTION:
            resp = json.dumps(
                {
                    "customer_id": f"cust_{idx:03d}",
                    "invoice_total": 125.50 + idx,
                    "currency": "USD",
                }
            )
            lat = (
                210.0 if tier == ModelTier.COMPACT else (580.0 if tier == ModelTier.MID else 1420.0)
            )
            return resp, base_out_tokens, lat + (idx % 9) * 12.0

        if item.category == TaskCategory.CLASSIFICATION:
            labels = ["BILLING", "TECHNICAL", "ACCOUNT"]
            resp = labels[idx % 3]
            lat = (
                160.0 if tier == ModelTier.COMPACT else (450.0 if tier == ModelTier.MID else 1150.0)
            )
            return resp, 12, lat + (idx % 7) * 10.0

        if item.category == TaskCategory.SUMMARIZATION:
            if tier == ModelTier.COMPACT and idx >= 38:
                # Compact misses required regex mitigation keyword on complex incidents 38..40
                resp = json.dumps({"root_cause": "network blip", "action_items": ["investigate"]})
            else:
                resp = json.dumps(
                    {
                        "root_cause": "quorum lease timeout under GC pause",
                        "action_items": ["apply mitigation patch", "verify rollback runbook"],
                    }
                )
            lat = (
                310.0 if tier == ModelTier.COMPACT else (790.0 if tier == ModelTier.MID else 1980.0)
            )
            return resp, base_out_tokens + 40, lat + (idx % 11) * 15.0

        if item.category == TaskCategory.CODE_SQL:
            if tier == ModelTier.COMPACT:
                # Compact omits CONCURRENTLY / lock_timeout and sets lock_safe=False
                resp = json.dumps(
                    {"sql_patch": "CREATE INDEX idx_orders ON orders(user_id);", "lock_safe": False}
                )
            else:
                resp = json.dumps(
                    {
                        "sql_patch": (
                            "SET lock_timeout = '2s'; "
                            "CREATE INDEX CONCURRENTLY idx_orders ON orders(user_id);"
                        ),
                        "lock_safe": True,
                    }
                )
            lat = (
                380.0 if tier == ModelTier.COMPACT else (940.0 if tier == ModelTier.MID else 2450.0)
            )
            return resp, base_out_tokens + 75, lat + (idx % 13) * 18.0

        # TaskCategory.REASONING (idx 52..60)
        if idx >= 59:
            # All 3 tiers fail on req-059 and req-060, but Frontier gets partial schema score (0.5)
            if tier == ModelTier.FRONTIER:
                resp = json.dumps(
                    {"invariant_holds": False, "proof_steps": ["step1"], "verified_quorum": 2}
                )
            else:
                resp = "unstructured reasoning scratchpad without valid JSON"
        elif tier == ModelTier.FRONTIER:
            resp = json.dumps(
                {
                    "invariant_holds": True,
                    "proof_steps": ["lemma_1_quorum_intersection", "lemma_2_fencing_token"],
                    "verified_quorum": 5,
                }
            )
        elif tier == ModelTier.MID:
            resp = json.dumps(
                {"invariant_holds": True, "proof_steps": ["sketch_only"], "verified_quorum": 2}
            )
        else:
            resp = "invalid non-json output"

        lat = 440.0 if tier == ModelTier.COMPACT else (1120.0 if tier == ModelTier.MID else 3180.0)
        return resp, base_out_tokens + 130, lat + (idx % 10) * 22.0

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        results: dict[tuple[str, ModelTier], ExecutionTrace] = {}
        for item in workload:
            p_hash = sha256_hex(item.prompt)
            for tier in TIER_ORDER:
                model_spec = catalog.models.get(tier)
                if model_spec is None:
                    results[(item.request_id, tier)] = ExecutionTrace(
                        request_id=item.request_id,
                        tier=tier,
                        model_id="unconfigured",
                        response_text="",
                        prompt_sha256=p_hash,
                        response_sha256=sha256_hex(""),
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=0.0,
                        cost_usd=Decimal("0"),
                        tokens_provenance=ProvenanceTag.UNKNOWN,
                        latency_provenance=ProvenanceTag.UNKNOWN,
                        cost_provenance=ProvenanceTag.UNKNOWN,
                        error_code="missing_tier_catalog_spec",
                    )
                    continue
                resp_text, out_tokens, lat_ms = self._simulate_response(item, tier)
                in_tokens = item.features.prompt_tokens_est
                cost = model_spec.compute_cost_usd(in_tokens, out_tokens)
                results[(item.request_id, tier)] = ExecutionTrace(
                    request_id=item.request_id,
                    tier=tier,
                    model_id=model_spec.model_id,
                    response_text=resp_text,
                    prompt_sha256=p_hash,
                    response_sha256=sha256_hex(resp_text),
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    latency_ms=round(lat_ms, 2),
                    cost_usd=cost,
                    tokens_provenance=ProvenanceTag.MODELED,
                    latency_provenance=ProvenanceTag.MODELED,
                    cost_provenance=ProvenanceTag.MODELED,
                )
        return results


def validate_endpoint_security(base_url: str, api_key: str | None) -> None:
    """Validate http/https scheme and block sending API keys over non-loopback plaintext HTTP."""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme {parsed.scheme!r}; only http and https allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Missing hostname in base_url {base_url!r}")
    if parsed.scheme == "http" and api_key and host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing to send Authorization header over plaintext http:// to non-loopback host {host!r}"
        )


class _BaseHTTPAdapter:
    """Shared async HTTP execution, retry, fail-fast, and spend circuit-breaker logic."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        max_concurrency: int = 8,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_cost_usd: Decimal = Decimal("5.00000000"),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_endpoint_security(base_url, api_key)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_concurrency = max(1, max_concurrency)
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._max_cost_usd = quantize_usd(max_cost_usd)
        self._transport = transport

    def _build_request(
        self, item: WorkloadItem, model_spec: TierModelSpec
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    def _parse_response(
        self,
        body: Any,
        item: WorkloadItem,
        model_spec: TierModelSpec,
        p_hash: str,
        elapsed_ms: float,
    ) -> tuple[ExecutionTrace, Decimal]:
        raise NotImplementedError

    async def _call_single(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        item: WorkloadItem,
        model_spec: TierModelSpec,
        tier_aborted: dict[ModelTier, str],
        running_spend: dict[str, Decimal],
        spend_lock: asyncio.Lock,
    ) -> ExecutionTrace:
        p_hash = sha256_hex(item.prompt)
        tier = model_spec.tier

        if tier in tier_aborted:
            return self._unknown_trace(
                item.request_id, tier, model_spec.model_id, p_hash, tier_aborted[tier]
            )

        async with spend_lock:
            if running_spend["total"] >= self._max_cost_usd:
                return self._unknown_trace(
                    item.request_id,
                    tier,
                    model_spec.model_id,
                    p_hash,
                    "max_cost_usd_circuit_breaker_tripped",
                )

        url, headers, payload = self._build_request(item, model_spec)

        async with sem:
            for attempt in range(self._max_retries + 1):
                if tier in tier_aborted:
                    return self._unknown_trace(
                        item.request_id, tier, model_spec.model_id, p_hash, tier_aborted[tier]
                    )
                start_ns = time.perf_counter_ns()
                try:
                    resp = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                        follow_redirects=False,
                    )
                except httpx.HTTPError as exc:
                    if attempt < self._max_retries:
                        await asyncio.sleep(0.05 * (2**attempt))
                        continue
                    return self._unknown_trace(
                        item.request_id,
                        tier,
                        model_spec.model_id,
                        p_hash,
                        f"http_transport_error:{type(exc).__name__}",
                    )

                elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000.0, 2)

                if resp.status_code in (401, 403, 404):
                    reason = f"fatal_http_{resp.status_code}"
                    tier_aborted[tier] = reason
                    return self._unknown_trace(
                        item.request_id, tier, model_spec.model_id, p_hash, reason
                    )

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < self._max_retries:
                        retry_after = resp.headers.get("Retry-After")
                        delay = 0.05 * (2**attempt)
                        if retry_after and retry_after.isdigit():
                            delay = min(float(retry_after), 2.0)
                        await asyncio.sleep(delay)
                        continue
                    return self._unknown_trace(
                        item.request_id,
                        tier,
                        model_spec.model_id,
                        p_hash,
                        f"http_status_{resp.status_code}",
                    )

                if resp.status_code != 200:
                    return self._unknown_trace(
                        item.request_id,
                        tier,
                        model_spec.model_id,
                        p_hash,
                        f"unexpected_http_{resp.status_code}",
                    )

                try:
                    body = resp.json()
                except ValueError:
                    return self._unknown_trace(
                        item.request_id,
                        tier,
                        model_spec.model_id,
                        p_hash,
                        "malformed_response_body",
                    )

                trace, cost = self._parse_response(body, item, model_spec, p_hash, elapsed_ms)
                if cost > Decimal("0"):
                    async with spend_lock:
                        running_spend["total"] = quantize_usd(running_spend["total"] + cost)
                return trace

        return self._unknown_trace(
            item.request_id, tier, model_spec.model_id, p_hash, "exhausted_retries"
        )

    @staticmethod
    def _unknown_trace(
        request_id: str, tier: ModelTier, model_id: str, prompt_sha256: str, error_code: str
    ) -> ExecutionTrace:
        return ExecutionTrace(
            request_id=request_id,
            tier=tier,
            model_id=model_id,
            response_text="",
            prompt_sha256=prompt_sha256,
            response_sha256=sha256_hex(""),
            input_tokens=0,
            output_tokens=0,
            latency_ms=0.0,
            cost_usd=Decimal("0"),
            tokens_provenance=ProvenanceTag.UNKNOWN,
            latency_provenance=ProvenanceTag.UNKNOWN,
            cost_provenance=ProvenanceTag.UNKNOWN,
            error_code=error_code,
        )

    async def execute_workload(
        self, workload: list[WorkloadItem], catalog: PricingCatalog
    ) -> dict[tuple[str, ModelTier], ExecutionTrace]:
        sem = asyncio.Semaphore(self._max_concurrency)
        tier_aborted: dict[ModelTier, str] = {}
        running_spend: dict[str, Decimal] = {"total": Decimal("0.00000000")}
        spend_lock = asyncio.Lock()
        results: dict[tuple[str, ModelTier], ExecutionTrace] = {}

        async with httpx.AsyncClient(
            timeout=self._timeout_seconds, transport=self._transport
        ) as client:
            tasks: list[tuple[str, ModelTier, asyncio.Task[ExecutionTrace]]] = []
            for item in workload:
                for tier in TIER_ORDER:
                    spec = catalog.models.get(tier)
                    if spec is None:
                        results[(item.request_id, tier)] = self._unknown_trace(
                            item.request_id,
                            tier,
                            "unconfigured",
                            sha256_hex(item.prompt),
                            "missing_tier_catalog_spec",
                        )
                        continue
                    t = asyncio.create_task(
                        self._call_single(
                            client, sem, item, spec, tier_aborted, running_spend, spend_lock
                        )
                    )
                    tasks.append((item.request_id, tier, t))

            for req_id, tier, task in tasks:
                results[(req_id, tier)] = await task

        return results


class OpenAICompatibleAdapter(_BaseHTTPAdapter):
    """Async live adapter for OpenAI-compatible and Ollama endpoints with strict safety guards."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        max_concurrency: int = 8,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_cost_usd: Decimal = Decimal("5.00000000"),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_cost_usd=max_cost_usd,
            transport=transport,
        )

    def _build_request(
        self, item: WorkloadItem, model_spec: TierModelSpec
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": model_spec.model_id,
            "messages": [{"role": "user", "content": item.prompt}],
            "temperature": 0.0,
        }
        return f"{self._base_url}/chat/completions", headers, payload

    def _parse_response(
        self,
        body: Any,
        item: WorkloadItem,
        model_spec: TierModelSpec,
        p_hash: str,
        elapsed_ms: float,
    ) -> tuple[ExecutionTrace, Decimal]:
        tier = model_spec.tier
        try:
            content = str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return (
                self._unknown_trace(
                    item.request_id,
                    tier,
                    model_spec.model_id,
                    p_hash,
                    "malformed_response_body",
                ),
                Decimal("0"),
            )

        usage = body.get("usage") if isinstance(body, dict) else None
        if (
            not isinstance(usage, dict)
            or not isinstance(usage.get("prompt_tokens"), int)
            or not isinstance(usage.get("completion_tokens"), int)
        ):
            return (
                ExecutionTrace(
                    request_id=item.request_id,
                    tier=tier,
                    model_id=model_spec.model_id,
                    response_text=content,
                    prompt_sha256=p_hash,
                    response_sha256=sha256_hex(content),
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=elapsed_ms,
                    cost_usd=Decimal("0"),
                    tokens_provenance=ProvenanceTag.UNKNOWN,
                    latency_provenance=ProvenanceTag.MEASURED,
                    cost_provenance=ProvenanceTag.UNKNOWN,
                    error_code="missing_usage_tokens",
                ),
                Decimal("0"),
            )

        in_tok = int(usage["prompt_tokens"])
        out_tok = int(usage["completion_tokens"])
        billed_usd = usage.get("cost_usd") or usage.get("total_cost_usd")
        if billed_usd is not None:
            try:
                cost = quantize_usd(billed_usd)
                cost_prov = ProvenanceTag.MEASURED
            except (ValueError, ArithmeticError):
                cost = model_spec.compute_cost_usd(in_tok, out_tok)
                cost_prov = ProvenanceTag.MODELED
        else:
            cost = model_spec.compute_cost_usd(in_tok, out_tok)
            cost_prov = ProvenanceTag.MODELED

        return (
            ExecutionTrace(
                request_id=item.request_id,
                tier=tier,
                model_id=model_spec.model_id,
                response_text=content,
                prompt_sha256=p_hash,
                response_sha256=sha256_hex(content),
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=elapsed_ms,
                cost_usd=cost,
                tokens_provenance=ProvenanceTag.MEASURED,
                latency_provenance=ProvenanceTag.MEASURED,
                cost_provenance=cost_prov,
            ),
            cost,
        )


class AnthropicAdapter(_BaseHTTPAdapter):
    """Async live adapter for Anthropic /v1/messages with strict safety and provenance guards."""

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        api_key: str | None = None,
        anthropic_version: str = "2023-06-01",
        max_tokens: int = 1024,
        max_concurrency: int = 8,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_cost_usd: Decimal = Decimal("5.00000000"),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            max_concurrency=max_concurrency,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_cost_usd=max_cost_usd,
            transport=transport,
        )
        self._anthropic_version = anthropic_version
        self._max_tokens = max(1, max_tokens)

    def _build_request(
        self, item: WorkloadItem, model_spec: TierModelSpec
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": self._anthropic_version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        payload: dict[str, Any] = {
            "model": model_spec.model_id,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": item.prompt}],
        }
        return f"{self._base_url}/v1/messages", headers, payload

    def _parse_response(
        self,
        body: Any,
        item: WorkloadItem,
        model_spec: TierModelSpec,
        p_hash: str,
        elapsed_ms: float,
    ) -> tuple[ExecutionTrace, Decimal]:
        tier = model_spec.tier
        try:
            content = str(body["content"][0]["text"])
        except (KeyError, IndexError, TypeError):
            return (
                self._unknown_trace(
                    item.request_id,
                    tier,
                    model_spec.model_id,
                    p_hash,
                    "malformed_response_body",
                ),
                Decimal("0"),
            )

        usage = body.get("usage") if isinstance(body, dict) else None
        in_tok_raw = usage.get("input_tokens") if isinstance(usage, dict) else None
        out_tok_raw = usage.get("output_tokens") if isinstance(usage, dict) else None
        if (
            not isinstance(in_tok_raw, int)
            or isinstance(in_tok_raw, bool)
            or not isinstance(out_tok_raw, int)
            or isinstance(out_tok_raw, bool)
        ):
            return (
                ExecutionTrace(
                    request_id=item.request_id,
                    tier=tier,
                    model_id=model_spec.model_id,
                    response_text=content,
                    prompt_sha256=p_hash,
                    response_sha256=sha256_hex(content),
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=elapsed_ms,
                    cost_usd=Decimal("0"),
                    tokens_provenance=ProvenanceTag.UNKNOWN,
                    latency_provenance=ProvenanceTag.MEASURED,
                    cost_provenance=ProvenanceTag.UNKNOWN,
                    error_code="missing_usage_tokens",
                ),
                Decimal("0"),
            )

        in_tok = int(in_tok_raw)
        out_tok = int(out_tok_raw)
        cost = model_spec.compute_cost_usd(in_tok, out_tok)

        return (
            ExecutionTrace(
                request_id=item.request_id,
                tier=tier,
                model_id=model_spec.model_id,
                response_text=content,
                prompt_sha256=p_hash,
                response_sha256=sha256_hex(content),
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=elapsed_ms,
                cost_usd=cost,
                tokens_provenance=ProvenanceTag.MEASURED,
                latency_provenance=ProvenanceTag.MEASURED,
                cost_provenance=ProvenanceTag.MODELED,
            ),
            cost,
        )
