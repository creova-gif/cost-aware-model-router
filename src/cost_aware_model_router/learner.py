from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ModelTier,
    TaskCategory,
    UsdDecimal,
    WorkloadItem,
    quantize_usd,
)


class ComplexityBucket(StrEnum):
    """Coarse complexity bucket derived from ``ComplexityFeatures.weighted_score()``."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def bucket_for_score(score: float) -> ComplexityBucket:
    """Map a complexity score in [0.0, 1.0] to a discrete ComplexityBucket."""
    if score < 0.30:
        return ComplexityBucket.LOW
    if score < 0.70:
        return ComplexityBucket.MEDIUM
    return ComplexityBucket.HIGH


class ArmStats(BaseModel):
    """Immutable per-(context, tier) bandit statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pulls: int = Field(default=0, ge=0)
    passes: int = Field(default=0, ge=0)
    total_cost_usd: UsdDecimal = Decimal("0.00000000")
    total_reward: float = 0.0

    @field_validator("total_cost_usd", mode="before")
    @classmethod
    def _validate_cost(cls, v: Any) -> Decimal:
        return quantize_usd(v)

    @property
    def pass_rate(self) -> float:
        """Empirical pass rate in [0.0, 1.0]; 0.0 when unpulled."""
        return self.passes / self.pulls if self.pulls > 0 else 0.0

    @property
    def mean_cost_usd(self) -> Decimal:
        """Mean cost per pull quantized to 8 decimal places; 0 when unpulled."""
        if self.pulls == 0:
            return Decimal("0.00000000")
        return quantize_usd(self.total_cost_usd / Decimal(self.pulls))


class ContextualBanditConfig(BaseModel):
    """Configuration for the quality-aware UCB1 contextual bandit router."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exploration_c: float = Field(default=0.35, ge=0.0)
    cost_penalty_lambda: float = Field(default=15.0, ge=0.0)
    min_quality_floor: float = Field(default=0.85, ge=0.0, le=1.0)
    seed: int = 42


type ContextKey = tuple[TaskCategory, ComplexityBucket]


def _encode_context(ctx: ContextKey) -> str:
    return f"{ctx[0].value}:{ctx[1].value}"


def _decode_context(raw: str) -> ContextKey:
    cat_str, bucket_str = raw.split(":", maxsplit=1)
    return (TaskCategory(cat_str), ComplexityBucket(bucket_str))


class _BanditSnapshot(BaseModel):
    """Serializable snapshot of ContextualBanditRouter state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: ContextualBanditConfig
    arms: dict[str, dict[ModelTier, ArmStats]]


class ContextualBanditRouter:
    """Zero-dependency online contextual bandit & quality-aware UCB1 router."""

    def __init__(self, config: ContextualBanditConfig | None = None) -> None:
        self._config: ContextualBanditConfig = config or ContextualBanditConfig()
        self._stats: dict[ContextKey, dict[ModelTier, ArmStats]] = {}

    @property
    def config(self) -> ContextualBanditConfig:
        """Return the immutable bandit configuration."""
        return self._config

    @staticmethod
    def context_for_item(item: WorkloadItem) -> ContextKey:
        """Derive the (TaskCategory, ComplexityBucket) context key for a workload item."""
        score = item.features.weighted_score()
        return (item.category, bucket_for_score(score))

    def _ensure_context(self, ctx: ContextKey) -> dict[ModelTier, ArmStats]:
        if ctx not in self._stats:
            self._stats[ctx] = {tier: ArmStats() for tier in TIER_ORDER}
        return self._stats[ctx]

    def get_arm_stats(self, ctx: ContextKey, tier: ModelTier) -> ArmStats:
        """Return current ArmStats for a (context, tier) pair."""
        arms = self._stats.get(ctx)
        if arms is None:
            return ArmStats()
        return arms.get(tier, ArmStats())

    def select_tier(self, item: WorkloadItem) -> tuple[ModelTier, str]:
        """Select a ModelTier using quality-constrained UCB1 and return an explanation."""
        ctx = self.context_for_item(item)
        arms = self._ensure_context(ctx)
        ctx_label = _encode_context(ctx)

        for tier in TIER_ORDER:
            if arms[tier].pulls == 0:
                return (
                    tier,
                    f"Cold-start exploration for context {ctx_label}: "
                    f"arm {tier.value} has 0 pulls.",
                )

        total_pulls = sum(arms[t].pulls for t in TIER_ORDER)
        log_total = math.log(total_pulls)

        ucb_scores: dict[ModelTier, float] = {}
        for tier in TIER_ORDER:
            arm = arms[tier]
            mean_reward = arm.total_reward / arm.pulls
            bonus = self._config.exploration_c * math.sqrt(log_total / arm.pulls)
            ucb_scores[tier] = mean_reward + bonus

        eligible = [
            tier
            for tier in TIER_ORDER
            if arms[tier].pass_rate + 1e-9 >= self._config.min_quality_floor
        ]
        if eligible:
            selected = max(
                eligible,
                key=lambda t: (ucb_scores[t], -TIER_ORDER.index(t)),
            )
            sel_arm = arms[selected]
            return (
                selected,
                f"UCB1 selected {selected.value} for {ctx_label} "
                f"(ucb={ucb_scores[selected]:.4f}, pass_rate={sel_arm.pass_rate:.3f} >= "
                f"floor={self._config.min_quality_floor:.2f}, "
                f"mean_cost=${sel_arm.mean_cost_usd:.6f}, pulls={sel_arm.pulls}).",
            )

        # No arm meets min_quality_floor: fall back to highest empirical pass_rate, then UCB1.
        selected = max(
            TIER_ORDER,
            key=lambda t: (arms[t].pass_rate, ucb_scores[t], -TIER_ORDER.index(t)),
        )
        sel_arm = arms[selected]
        return (
            selected,
            f"Quality-floor fallback selected {selected.value} for {ctx_label} "
            f"(no arm met floor={self._config.min_quality_floor:.2f}; "
            f"best pass_rate={sel_arm.pass_rate:.3f}, ucb={ucb_scores[selected]:.4f}, "
            f"pulls={sel_arm.pulls}).",
        )

    def update(
        self,
        item: WorkloadItem,
        tier: ModelTier,
        *,
        passed: bool,
        cost_usd: Decimal,
    ) -> None:
        """Update arm statistics for the item's context and selected tier."""
        ctx = self.context_for_item(item)
        arms = self._ensure_context(ctx)
        prev = arms[tier]
        q_cost = quantize_usd(cost_usd)
        reward = (1.0 if passed else 0.0) - self._config.cost_penalty_lambda * float(q_cost)
        arms[tier] = ArmStats(
            pulls=prev.pulls + 1,
            passes=prev.passes + (1 if passed else 0),
            total_cost_usd=quantize_usd(prev.total_cost_usd + q_cost),
            total_reward=prev.total_reward + reward,
        )

    def export_learned_thresholds(self) -> dict[str, str]:
        """Export a deterministic mapping of observed context keys to best learned tier."""
        exported: dict[str, str] = {}
        for ctx in sorted(self._stats.keys(), key=_encode_context):
            arms = self._stats[ctx]
            pulled = [t for t in TIER_ORDER if arms[t].pulls > 0]
            if not pulled:
                continue
            eligible = [
                t
                for t in pulled
                if arms[t].pass_rate + 1e-9 >= self._config.min_quality_floor
            ]
            pool = eligible if eligible else pulled
            best = max(
                pool,
                key=lambda t: (
                    arms[t].pass_rate if not eligible else 0.0,
                    arms[t].total_reward / arms[t].pulls,
                    -TIER_ORDER.index(t),
                ),
            )
            exported[_encode_context(ctx)] = best.value
        return exported

    def to_json(self) -> str:
        """Serialize router configuration and arm statistics to deterministic JSON."""
        sorted_arms: dict[str, dict[ModelTier, ArmStats]] = {}
        for ctx in sorted(self._stats.keys(), key=_encode_context):
            arms = self._stats[ctx]
            sorted_arms[_encode_context(ctx)] = {tier: arms[tier] for tier in TIER_ORDER}
        snapshot = _BanditSnapshot(config=self._config, arms=sorted_arms)
        return snapshot.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str) -> ContextualBanditRouter:
        """Reconstruct a ContextualBanditRouter from a JSON snapshot."""
        snapshot = _BanditSnapshot.model_validate_json(raw)
        router = cls(config=snapshot.config)
        for ctx_str, tier_map in snapshot.arms.items():
            ctx = _decode_context(ctx_str)
            router._stats[ctx] = {
                tier: tier_map.get(tier, ArmStats()) for tier in TIER_ORDER
            }
        return router
