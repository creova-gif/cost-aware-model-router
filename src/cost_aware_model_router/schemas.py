from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

USD_QUANT = Decimal("0.00000001")


def quantize_usd(value: Decimal | int | float | str) -> Decimal:
    """Quantize a monetary USD amount to 8 decimal places with ROUND_HALF_UP."""
    dec = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not dec.is_finite():
        raise ValueError("USD amount must be a finite Decimal")
    return dec.quantize(USD_QUANT, rounding=ROUND_HALF_UP)


def format_usd_str(value: Decimal) -> str:
    return f"{quantize_usd(value):.8f}"


UsdDecimal = Annotated[
    Decimal,
    PlainSerializer(lambda v: format_usd_str(v), return_type=str, when_used="json"),
]


class ModelTier(StrEnum):
    COMPACT = "compact"
    MID = "mid"
    FRONTIER = "frontier"


TIER_ORDER: tuple[ModelTier, ...] = (
    ModelTier.COMPACT,
    ModelTier.MID,
    ModelTier.FRONTIER,
)


class ProvenanceTag(StrEnum):
    MEASURED = "measured"
    MODELED = "modeled"
    UNKNOWN = "unknown"


class TaskCategory(StrEnum):
    JSON_EXTRACTION = "json_extraction"
    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    CODE_SQL = "code_sql"
    REASONING = "reasoning"


class EvalType(StrEnum):
    JSON_SCHEMA = "json_schema"
    REGEX_MATCH = "regex_match"
    EXACT_MATCH = "exact_match"
    JSON_FIELD_PREDICATE = "json_field_predicate"
    LATENCY_SLA = "latency_sla"


class EvalRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_type: EvalType
    # For json_schema: required keys & expected field types {"field": "str"|"int"|"number"|"bool"|"list"|"dict"}
    schema_fields: dict[str, str] | None = None
    # For regex_match: bounded regex pattern (<= 256 chars, no nested catastrophic quantifiers)
    pattern: str | None = None
    case_sensitive: bool = True
    # For exact_match: normalized target string or allowed set
    expected_text: str | None = None
    allowed_values: list[str] | None = None
    # For json_field_predicate: declarative predicates {"field.path": {"eq": ..., "min": ..., "max": ..., "contains": ...}}
    predicates: dict[str, dict[str, Any]] | None = None
    # For latency_sla: maximum allowed latency in milliseconds
    max_latency_ms: float | None = None
    weight: float = Field(default=1.0, gt=0.0)


class ComplexityFeatures(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens_est: int = Field(ge=1)
    has_code_or_sql: bool = False
    schema_depth: int = Field(default=0, ge=0)
    multi_step_reasoning: bool = False
    domain_difficulty: float = Field(default=0.3, ge=0.0, le=1.0)

    def weighted_score(self) -> float:
        """Deterministic transparent complexity score in [0.0, 1.0]."""
        token_component = min(self.prompt_tokens_est / 1200.0, 1.0) * 0.20
        code_component = 0.25 if self.has_code_or_sql else 0.0
        schema_component = min(self.schema_depth / 4.0, 1.0) * 0.15
        step_component = 0.20 if self.multi_step_reasoning else 0.0
        domain_component = self.domain_difficulty * 0.20
        raw = (
            token_component + code_component + schema_component + step_component + domain_component
        )
        return round(min(max(raw, 0.0), 1.0), 6)


class WorkloadItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    org_id: str = Field(default="org_demo")
    agent_id: str = Field(default="agent_default")
    category: TaskCategory
    prompt: str = Field(min_length=1)
    features: ComplexityFeatures
    evaluators: list[EvalRule] = Field(min_length=1)
    otel_attributes: dict[str, str] = Field(default_factory=dict)


class TierModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ModelTier
    model_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    input_usd_per_1m: UsdDecimal
    output_usd_per_1m: UsdDecimal
    price_source: str = Field(min_length=1)

    @field_validator("input_usd_per_1m", "output_usd_per_1m", mode="before")
    @classmethod
    def _validate_usd(cls, v: Any) -> Decimal:
        dec = quantize_usd(v)
        if dec < Decimal("0"):
            raise ValueError("Token price cannot be negative")
        return dec

    def compute_cost_usd(self, input_tokens: int, output_tokens: int) -> Decimal:
        in_cost = (Decimal(input_tokens) * self.input_usd_per_1m) / Decimal(1_000_000)
        out_cost = (Decimal(output_tokens) * self.output_usd_per_1m) / Decimal(1_000_000)
        return quantize_usd(in_cost + out_cost)


class PricingCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: str = Field(min_length=1)
    models: dict[ModelTier, TierModelSpec]


class MetricProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_provenance: ProvenanceTag
    latency_provenance: ProvenanceTag
    verdict_provenance: ProvenanceTag
    cost_provenance: ProvenanceTag

    @property
    def is_any_unknown(self) -> bool:
        return ProvenanceTag.UNKNOWN in (
            self.tokens_provenance,
            self.latency_provenance,
            self.verdict_provenance,
            self.cost_provenance,
        )

    @property
    def summary_tag(self) -> ProvenanceTag:
        if self.is_any_unknown:
            return ProvenanceTag.UNKNOWN
        if (
            self.tokens_provenance == ProvenanceTag.MEASURED
            and self.latency_provenance == ProvenanceTag.MEASURED
            and self.verdict_provenance == ProvenanceTag.MEASURED
        ):
            return ProvenanceTag.MEASURED
        return ProvenanceTag.MODELED


class ExecutionTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    tier: ModelTier
    model_id: str
    response_text: str
    prompt_sha256: str
    response_sha256: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)
    cost_usd: UsdDecimal
    tokens_provenance: ProvenanceTag
    latency_provenance: ProvenanceTag
    cost_provenance: ProvenanceTag
    error_code: str | None = None

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class EvalVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    tier: ModelTier
    passed: bool
    content_passed: bool
    latency_passed: bool
    score: float = Field(ge=0.0, le=1.0)
    content_score: float = Field(ge=0.0, le=1.0)
    verdict_provenance: ProvenanceTag
    failure_reasons: list[str] = Field(default_factory=list)


class CellEvaluation(BaseModel):
    """Redacted per-(request_id, tier) record safe for embedding in receipts and HTML."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    category: TaskCategory
    tier: ModelTier
    model_id: str
    prompt_sha256: str
    response_sha256: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: UsdDecimal
    passed: bool
    content_passed: bool
    latency_passed: bool
    score: float
    content_score: float
    provenance: MetricProvenance
    failure_reasons: list[str] = Field(default_factory=list)
    error_code: str | None = None

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class PolicyKind(StrEnum):
    STATIC_TIER = "static_tier"
    COMPLEXITY_THRESHOLD = "complexity_threshold"
    CASCADE_ESCALATION = "cascade_escalation"
    EAR_ORACLE = "ear_oracle"


class PolicySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    kind: PolicyKind
    deployable: bool = True
    in_sample_fit: bool = False
    # For static_tier
    target_tier: ModelTier | None = None
    # For complexity_threshold: score < compact_max -> COMPACT; < mid_max -> MID; else FRONTIER
    compact_max_score: float | None = Field(default=None, ge=0.0, le=1.0)
    mid_max_score: float | None = Field(default=None, ge=0.0, le=1.0)
    # For ear_oracle: equivalence margin tau in [0.0, 1.0]
    tau: float | None = Field(default=None, ge=0.0, le=1.0)


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    category: TaskCategory
    policy_id: str
    selected_tier: ModelTier
    attempted_tiers: list[ModelTier]
    cost_usd: UsdDecimal
    baseline_cost_usd: UsdDecimal
    latency_ms: float
    passed: bool
    score: float
    frontier_required: bool
    regressed_vs_baseline: bool
    oracle_all_failed: bool = False
    provenance: MetricProvenance
    explanation: str

    @field_validator("cost_usd", "baseline_cost_usd", mode="before")
    @classmethod
    def _validate_costs(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class ProvenanceBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    measured_requests: int = 0
    measured_cost_usd: UsdDecimal = Decimal("0.00000000")
    modeled_requests: int = 0
    modeled_cost_usd: UsdDecimal = Decimal("0.00000000")
    unknown_excluded_requests: int = 0
    unknown_burned_cost_usd: UsdDecimal = Decimal("0.00000000")

    @field_validator(
        "measured_cost_usd", "modeled_cost_usd", "unknown_burned_cost_usd", mode="before"
    )
    @classmethod
    def _validate_costs(cls, v: Any) -> Decimal:
        return quantize_usd(v)


class PolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    display_name: str
    kind: PolicyKind
    deployable: bool
    in_sample_fit: bool
    evaluated_requests: int
    excluded_unknown_requests: int
    passed_count: int
    pass_rate: float
    total_cost_usd: UsdDecimal
    cost_savings_vs_baseline_usd: UsdDecimal
    cost_savings_vs_baseline_pct: float
    cost_per_successful_outcome_usd: UsdDecimal | None
    p95_latency_ms: float
    tier_counts: dict[ModelTier, int]
    is_pareto_optimal: bool = False
    provenance_breakdown: ProvenanceBreakdown
    decisions: list[RoutingDecision]

    @field_validator(
        "total_cost_usd",
        "cost_savings_vs_baseline_usd",
        mode="before",
    )
    @classmethod
    def _validate_costs(cls, v: Any) -> Decimal:
        return quantize_usd(v)

    @field_validator("cost_per_successful_outcome_usd", mode="before")
    @classmethod
    def _validate_goodput(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return quantize_usd(v)


class ReceiptIntegrity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["sha256"] = "sha256"
    canonicalization: Literal["sorted-keys-utf8-compact-json-decimal-strings"] = (
        "sorted-keys-utf8-compact-json-decimal-strings"
    )
    note: str = (
        "Unsigned SHA-256 digest detects accidental corruption or payload drift; "
        "not cryptographic non-repudiation."
    )
    digest: str = Field(min_length=64, max_length=64)


class SavingsReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_version: Literal["1.0.0"] = "1.0.0"
    org_id: str
    workload_name: str
    workload_sha256: str
    catalog_version: str
    total_workload_requests: int
    complete_case_requests: int
    excluded_unknown_requests: int
    baseline_policy_id: str
    recommended_policy_id: str
    oracle_policy_id: str
    pareto_optimal_policy_ids: list[str]
    catalog: PricingCatalog
    evaluation_matrix: list[CellEvaluation]
    policy_results: list[PolicyResult]
    integrity: ReceiptIntegrity
