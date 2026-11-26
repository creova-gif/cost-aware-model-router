from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import Any, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cost_aware_model_router.resilience import BoundedTTLCache
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ComplexityFeatures,
    ModelTier,
    ProvenanceTag,
    TaskCategory,
    UsdDecimal,
    format_usd_str,
    quantize_usd,
)


def normalize_prompt(prompt: str) -> str:
    """Deterministic whitespace and case-preserving prompt normalization."""
    return " ".join(prompt.strip().split())


def compute_prompt_sha256(prompt: str) -> str:
    """Compute SHA-256 hex digest of normalized prompt text."""
    normalized = normalize_prompt(prompt)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tier_rank(tier: ModelTier) -> int:
    """Return ordinal rank of a ModelTier in TIER_ORDER (0=COMPACT, 1=MID, 2=FRONTIER)."""
    return TIER_ORDER.index(tier)


class ThermostatAction(StrEnum):
    ALLOW_REQUESTED_TIER = "allow_requested_tier"
    CACHE_HIT = "cache_hit"
    STEP_DOWN_TIER = "step_down_tier"
    QUALITY_FLOOR_ESCALATION = "quality_floor_escalation"
    LOOP_CIRCUIT_BREAKER = "loop_circuit_breaker"
    BUDGET_CIRCUIT_BREAKER = "budget_circuit_breaker"


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1)
    agent_id: str | None = None
    hourly_budget_usd: UsdDecimal
    soft_downgrade_ratio: float = Field(default=0.80, gt=0.0, le=1.0)
    hard_cap_ratio: float = Field(default=1.00, gt=0.0, le=2.0)
    max_loop_repeats: int = Field(default=3, ge=2)
    min_quality_tier: ModelTier = ModelTier.COMPACT
    enable_semantic_cache: bool = True
    loop_window_size: int = Field(default=32, ge=2)

    @field_validator("hourly_budget_usd", mode="before")
    @classmethod
    def _validate_budget(cls, v: Any) -> Decimal:
        dec = quantize_usd(v)
        if dec <= Decimal("0"):
            raise ValueError("hourly_budget_usd must be strictly positive")
        return dec

    @model_validator(mode="after")
    def _validate_ratios(self) -> BudgetPolicy:
        if self.soft_downgrade_ratio >= self.hard_cap_ratio:
            raise ValueError("soft_downgrade_ratio must be strictly less than hard_cap_ratio")
        return self


class ThermostatDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    requested_tier: ModelTier
    approved_tier: ModelTier | None
    action: ThermostatAction
    allowed: bool
    cached_response: str | None = None
    budget_utilization_ratio: float = Field(ge=0.0)
    remaining_budget_usd: UsdDecimal
    explanation: str = Field(min_length=1)

    @field_validator("remaining_budget_usd", mode="before")
    @classmethod
    def _validate_remaining_usd(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class SpendLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    category: TaskCategory
    tier: ModelTier
    provenance: ProvenanceTag
    passed: bool
    cost_usd: UsdDecimal
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached: bool = False
    otel_attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, v: Any) -> Decimal:
        dec = quantize_usd(v)
        if dec < Decimal("0"):
            raise ValueError("cost_usd cannot be negative")
        return dec


class SpendSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_requests: int = Field(ge=0)
    passed_requests: int = Field(ge=0)
    failed_requests: int = Field(ge=0)
    cache_hits: int = Field(default=0, ge=0)
    total_cost_usd: UsdDecimal
    measured_cost_usd: UsdDecimal
    modeled_cost_usd: UsdDecimal
    unknown_cost_usd: UsdDecimal = Decimal("0.00000000")
    cost_per_request_usd: UsdDecimal
    cost_per_successful_outcome_usd: UsdDecimal | None
    spend_by_org: dict[str, UsdDecimal]
    spend_by_agent: dict[str, UsdDecimal]
    spend_by_category: dict[TaskCategory, UsdDecimal]
    spend_by_tier: dict[ModelTier, UsdDecimal]

    @field_validator(
        "total_cost_usd",
        "measured_cost_usd",
        "modeled_cost_usd",
        "unknown_cost_usd",
        "cost_per_request_usd",
        mode="before",
    )
    @classmethod
    def _validate_usd_fields(cls, v: Any) -> Decimal:
        return quantize_usd(v)

    @field_validator("cost_per_successful_outcome_usd", mode="before")
    @classmethod
    def _validate_goodput_usd(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return quantize_usd(v)


class AgentBudgetThermostat:
    """Real-time agent budget thermostat, loop breaker, cache, and unit-economics ledger."""

    def __init__(
        self,
        policies: list[BudgetPolicy] | None = None,
        *,
        cache_max_entries: int = 1024,
        cache_ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policies: dict[tuple[str, str | None], BudgetPolicy] = {}
        self._spend_by_scope: dict[tuple[str, str | None], Decimal] = {}
        self._loop_windows: dict[tuple[str, str], deque[str]] = {}
        self._response_cache = BoundedTTLCache(
            max_entries=cache_max_entries,
            ttl_seconds=cache_ttl_seconds,
            clock=clock,
        )
        self._ledger: list[SpendLedgerEntry] = []

        if policies:
            for policy in policies:
                self.register_policy(policy)

    def register_policy(self, policy: BudgetPolicy) -> None:
        """Register or update a budget policy for (org_id, agent_id) or (org_id, None)."""
        key = (policy.org_id, policy.agent_id)
        self._policies[key] = policy
        if key not in self._spend_by_scope:
            self._spend_by_scope[key] = Decimal("0.00000000")

    def get_policy(self, org_id: str, agent_id: str) -> BudgetPolicy:
        """Resolve agent-specific policy first, falling back to org-wide default policy."""
        agent_key = (org_id, agent_id)
        if agent_key in self._policies:
            return self._policies[agent_key]
        org_key = (org_id, None)
        if org_key in self._policies:
            return self._policies[org_key]
        default_policy = BudgetPolicy(
            org_id=org_id,
            agent_id=agent_id,
            hourly_budget_usd=Decimal("10.00000000"),
        )
        self.register_policy(default_policy)
        return default_policy

    def current_spend_usd(self, org_id: str, agent_id: str) -> Decimal:
        """Return accumulated spend for the resolved policy scope."""
        policy = self.get_policy(org_id, agent_id)
        scope_key = (policy.org_id, policy.agent_id)
        return quantize_usd(self._spend_by_scope.get(scope_key, Decimal("0.00000000")))

    def evaluate_request(
        self,
        *,
        request_id: str,
        org_id: str,
        agent_id: str,
        prompt: str,
        requested_tier: ModelTier,
        min_quality_tier: ModelTier | None = None,
        features: ComplexityFeatures | None = None,
        estimated_cost_usd: Decimal | None = None,
    ) -> ThermostatDecision:
        """Evaluate an incoming agent request against loop detection, cache, and budget bounds."""
        policy = self.get_policy(org_id, agent_id)
        scope_key = (policy.org_id, policy.agent_id)
        spent = self._spend_by_scope.get(scope_key, Decimal("0.00000000"))
        projected_spend = (
            spent + quantize_usd(estimated_cost_usd) if estimated_cost_usd is not None else spent
        )

        budget = policy.hourly_budget_usd
        utilization = round(float(projected_spend / budget), 6)
        remaining = quantize_usd(max(budget - spent, Decimal("0.00000000")))

        prompt_hash = compute_prompt_sha256(prompt)
        loop_key = (org_id, agent_id)
        window = self._loop_windows.get(loop_key)
        if window is None:
            window = deque(maxlen=policy.loop_window_size)
            self._loop_windows[loop_key] = window
        window.append(prompt_hash)

        repeat_count = sum(1 for h in window if h == prompt_hash)
        if repeat_count >= policy.max_loop_repeats:
            return ThermostatDecision(
                org_id=org_id,
                agent_id=agent_id,
                request_id=request_id,
                requested_tier=requested_tier,
                approved_tier=None,
                action=ThermostatAction.LOOP_CIRCUIT_BREAKER,
                allowed=False,
                cached_response=None,
                budget_utilization_ratio=utilization,
                remaining_budget_usd=remaining,
                explanation=(
                    f"Runaway loop circuit breaker tripped for ({org_id}, {agent_id}): "
                    f"prompt hash {prompt_hash[:12]} repeated {repeat_count} times "
                    f"(limit={policy.max_loop_repeats})."
                ),
            )

        if policy.enable_semantic_cache:
            cached_text = self._response_cache.get(org_id, prompt_hash)
            if cached_text is not None:
                return ThermostatDecision(
                    org_id=org_id,
                    agent_id=agent_id,
                    request_id=request_id,
                    requested_tier=requested_tier,
                    approved_tier=requested_tier,
                    action=ThermostatAction.CACHE_HIT,
                    allowed=True,
                    cached_response=cached_text,
                    budget_utilization_ratio=utilization,
                    remaining_budget_usd=remaining,
                    explanation=(
                        f"Deterministic prompt cache hit for org={org_id} "
                        f"(sha256={prompt_hash[:12]}), marginal inference cost $0.00000000."
                    ),
                )

        if utilization >= policy.hard_cap_ratio:
            return ThermostatDecision(
                org_id=org_id,
                agent_id=agent_id,
                request_id=request_id,
                requested_tier=requested_tier,
                approved_tier=None,
                action=ThermostatAction.BUDGET_CIRCUIT_BREAKER,
                allowed=False,
                cached_response=None,
                budget_utilization_ratio=utilization,
                remaining_budget_usd=remaining,
                explanation=(
                    f"Hard budget cap reached for ({org_id}, {agent_id}): "
                    f"utilization {utilization:.2%} >= {policy.hard_cap_ratio:.2%} "
                    f"of ${format_usd_str(budget)} hourly budget."
                ),
            )

        effective_floor = policy.min_quality_tier
        if min_quality_tier is not None and tier_rank(min_quality_tier) > tier_rank(
            effective_floor
        ):
            effective_floor = min_quality_tier
        if (
            features is not None
            and features.weighted_score() >= 0.80
            and tier_rank(ModelTier.MID) > tier_rank(effective_floor)
        ):
            effective_floor = ModelTier.MID

        if utilization >= policy.soft_downgrade_ratio:
            req_idx = tier_rank(requested_tier)
            floor_idx = tier_rank(effective_floor)
            candidate_idx = req_idx - 1
            if candidate_idx < 0 or candidate_idx < floor_idx:
                return ThermostatDecision(
                    org_id=org_id,
                    agent_id=agent_id,
                    request_id=request_id,
                    requested_tier=requested_tier,
                    approved_tier=None,
                    action=ThermostatAction.QUALITY_FLOOR_ESCALATION,
                    allowed=False,
                    cached_response=None,
                    budget_utilization_ratio=utilization,
                    remaining_budget_usd=remaining,
                    explanation=(
                        f"Budget utilization {utilization:.2%} >= soft downgrade threshold "
                        f"{policy.soft_downgrade_ratio:.2%}, but stepping down from "
                        f"{requested_tier.value} would violate minimum quality floor "
                        f"{effective_floor.value}. Escalating for human/budget approval."
                    ),
                )
            downgraded_tier = TIER_ORDER[candidate_idx]
            return ThermostatDecision(
                org_id=org_id,
                agent_id=agent_id,
                request_id=request_id,
                requested_tier=requested_tier,
                approved_tier=downgraded_tier,
                action=ThermostatAction.STEP_DOWN_TIER,
                allowed=True,
                cached_response=None,
                budget_utilization_ratio=utilization,
                remaining_budget_usd=remaining,
                explanation=(
                    f"Budget utilization {utilization:.2%} >= soft downgrade threshold "
                    f"{policy.soft_downgrade_ratio:.2%}; gracefully stepped down from "
                    f"{requested_tier.value} to {downgraded_tier.value} "
                    f"(quality floor={effective_floor.value})."
                ),
            )

        return ThermostatDecision(
            org_id=org_id,
            agent_id=agent_id,
            request_id=request_id,
            requested_tier=requested_tier,
            approved_tier=requested_tier,
            action=ThermostatAction.ALLOW_REQUESTED_TIER,
            allowed=True,
            cached_response=None,
            budget_utilization_ratio=utilization,
            remaining_budget_usd=remaining,
            explanation=(
                f"Approved {requested_tier.value} within budget "
                f"(utilization={utilization:.2%}, remaining=${format_usd_str(remaining)})."
            ),
        )

    def record_outcome(
        self,
        *,
        request_id: str,
        org_id: str,
        agent_id: str,
        category: TaskCategory,
        tier: ModelTier,
        provenance: ProvenanceTag,
        passed: bool,
        cost_usd: Decimal | int | float | str,
        input_tokens: int,
        output_tokens: int,
        prompt: str | None = None,
        response_text: str | None = None,
        cached: bool = False,
        otel_attributes: dict[str, str] | None = None,
    ) -> SpendLedgerEntry:
        """Record actual request spend, outcome quality, and populate semantic cache if passed."""
        quantized_cost = Decimal("0.00000000") if cached else quantize_usd(cost_usd)
        policy = self.get_policy(org_id, agent_id)
        scope_key = (policy.org_id, policy.agent_id)
        self._spend_by_scope[scope_key] = quantize_usd(
            self._spend_by_scope.get(scope_key, Decimal("0.00000000")) + quantized_cost
        )

        if (
            policy.enable_semantic_cache
            and passed
            and prompt is not None
            and response_text is not None
        ):
            prompt_hash = compute_prompt_sha256(prompt)
            self._response_cache.set(org_id, prompt_hash, response_text)

        entry = SpendLedgerEntry(
            request_id=request_id,
            org_id=org_id,
            agent_id=agent_id,
            category=category,
            tier=tier,
            provenance=provenance,
            passed=passed,
            cost_usd=quantized_cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached=cached,
            otel_attributes=otel_attributes or {},
        )
        self._ledger.append(entry)
        return entry

    @property
    def ledger_entries(self) -> list[SpendLedgerEntry]:
        """Return immutable copy of recorded spend ledger entries."""
        return list(self._ledger)

    def summarize_economics(self, org_id: str | None = None) -> SpendSummary:
        """Compute live unit economics across all or filtered org ledger entries."""
        entries = [e for e in self._ledger if org_id is None or e.org_id == org_id]
        total_requests = len(entries)
        passed_requests = sum(1 for e in entries if e.passed)
        failed_requests = total_requests - passed_requests
        cache_hits = sum(1 for e in entries if e.cached)

        total_cost = Decimal("0.00000000")
        measured_cost = Decimal("0.00000000")
        modeled_cost = Decimal("0.00000000")
        unknown_cost = Decimal("0.00000000")

        spend_by_org: dict[str, Decimal] = {}
        spend_by_agent: dict[str, Decimal] = {}
        spend_by_category: dict[TaskCategory, Decimal] = {
            cat: Decimal("0.00000000") for cat in TaskCategory
        }
        spend_by_tier: dict[ModelTier, Decimal] = {
            tier: Decimal("0.00000000") for tier in TIER_ORDER
        }

        for entry in entries:
            cost = entry.cost_usd
            total_cost = quantize_usd(total_cost + cost)

            match entry.provenance:
                case ProvenanceTag.MEASURED:
                    measured_cost = quantize_usd(measured_cost + cost)
                case ProvenanceTag.MODELED:
                    modeled_cost = quantize_usd(modeled_cost + cost)
                case ProvenanceTag.UNKNOWN:
                    unknown_cost = quantize_usd(unknown_cost + cost)
                case _ as unreachable:
                    assert_never(unreachable)

            spend_by_org[entry.org_id] = quantize_usd(
                spend_by_org.get(entry.org_id, Decimal("0.00000000")) + cost
            )
            agent_scope = f"{entry.org_id}/{entry.agent_id}"
            spend_by_agent[agent_scope] = quantize_usd(
                spend_by_agent.get(agent_scope, Decimal("0.00000000")) + cost
            )
            spend_by_category[entry.category] = quantize_usd(
                spend_by_category[entry.category] + cost
            )
            spend_by_tier[entry.tier] = quantize_usd(spend_by_tier[entry.tier] + cost)

        cost_per_request = (
            quantize_usd(total_cost / Decimal(total_requests))
            if total_requests > 0
            else Decimal("0.00000000")
        )
        cost_per_successful_outcome: Decimal | None = (
            quantize_usd(total_cost / Decimal(passed_requests)) if passed_requests > 0 else None
        )

        return SpendSummary(
            total_requests=total_requests,
            passed_requests=passed_requests,
            failed_requests=failed_requests,
            cache_hits=cache_hits,
            total_cost_usd=total_cost,
            measured_cost_usd=measured_cost,
            modeled_cost_usd=modeled_cost,
            unknown_cost_usd=unknown_cost,
            cost_per_request_usd=cost_per_request,
            cost_per_successful_outcome_usd=cost_per_successful_outcome,
            spend_by_org=spend_by_org,
            spend_by_agent=spend_by_agent,
            spend_by_category=spend_by_category,
            spend_by_tier=spend_by_tier,
        )
