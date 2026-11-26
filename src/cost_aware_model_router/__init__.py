from __future__ import annotations

from cost_aware_model_router.adapters import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
)
from cost_aware_model_router.dashboard import render_live_finops_dashboard_html
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    OpenAIRouterGateway,
    ShadowComparisonRecord,
    ThermostatBlockedError,
)
from cost_aware_model_router.governance import (
    AuthDecision,
    TenantAuthRegistry,
    TenantKeyRecord,
    TenantRole,
    TokenBucketRateLimiter,
)
from cost_aware_model_router.judge import (
    DeterministicRubricJudge,
    HoldoutGeneralizationReport,
    JudgeVerdict,
    LLMJudge,
    OutOfSamplePolicyAuditor,
    RubricCriterion,
)
from cost_aware_model_router.learner import (
    ArmStats,
    ComplexityBucket,
    ContextualBanditConfig,
    ContextualBanditRouter,
)
from cost_aware_model_router.metrics import render_prometheus_metrics
from cost_aware_model_router.registry import MultiProviderAdapter
from cost_aware_model_router.resilience import (
    BoundedTTLCache,
    CircuitBreakerConfig,
    CircuitState,
    FallbackChainAdapter,
    ProviderCircuitBreaker,
)
from cost_aware_model_router.schemas import (
    ModelTier,
    PolicyKind,
    PolicyResult,
    PolicySpec,
    PricingCatalog,
    ProvenanceTag,
    SavingsReceipt,
    TaskCategory,
    WorkloadItem,
)
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.telemetry import InMemorySpanExporter, OTelSpanRecord
from cost_aware_model_router.thermostat import (
    AgentBudgetThermostat,
    BudgetPolicy,
    SpendLedgerEntry,
    SpendSummary,
    ThermostatAction,
    ThermostatDecision,
)


def healthcheck() -> str:
    return "ok"


__all__ = [
    "AgentBudgetThermostat",
    "AnthropicAdapter",
    "ArmStats",
    "AuthDecision",
    "BoundedTTLCache",
    "BudgetPolicy",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatMessage",
    "CircuitBreakerConfig",
    "CircuitState",
    "ComplexityBucket",
    "ContextualBanditConfig",
    "ContextualBanditRouter",
    "DeterministicFixtureAdapter",
    "DeterministicRubricJudge",
    "FallbackChainAdapter",
    "HoldoutGeneralizationReport",
    "InMemorySpanExporter",
    "JudgeVerdict",
    "LLMJudge",
    "ModelTier",
    "MultiProviderAdapter",
    "OTelSpanRecord",
    "OpenAICompatibleAdapter",
    "OpenAIRouterGateway",
    "OutOfSamplePolicyAuditor",
    "PolicyKind",
    "PolicyResult",
    "PolicySpec",
    "PricingCatalog",
    "ProvenanceTag",
    "ProviderAdapter",
    "ProviderCircuitBreaker",
    "RubricCriterion",
    "SQLiteEconomicsStore",
    "SavingsReceipt",
    "ShadowComparisonRecord",
    "SpendLedgerEntry",
    "SpendSummary",
    "TaskCategory",
    "TenantAuthRegistry",
    "TenantKeyRecord",
    "TenantRole",
    "ThermostatAction",
    "ThermostatBlockedError",
    "ThermostatDecision",
    "TokenBucketRateLimiter",
    "WorkloadItem",
    "create_router_http_server",
    "healthcheck",
    "render_live_finops_dashboard_html",
    "render_prometheus_metrics",
]
