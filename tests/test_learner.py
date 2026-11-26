from __future__ import annotations

from decimal import Decimal

from cost_aware_model_router.learner import (
    ArmStats,
    ComplexityBucket,
    ContextualBanditConfig,
    ContextualBanditRouter,
    bucket_for_score,
)
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ComplexityFeatures,
    EvalRule,
    EvalType,
    ModelTier,
    TaskCategory,
    WorkloadItem,
)


def _make_item(
    request_id: str,
    category: TaskCategory,
    *,
    prompt_tokens_est: int = 100,
    has_code_or_sql: bool = False,
    schema_depth: int = 0,
    multi_step_reasoning: bool = False,
    domain_difficulty: float = 0.1,
) -> WorkloadItem:
    return WorkloadItem(
        request_id=request_id,
        category=category,
        prompt="test prompt",
        features=ComplexityFeatures(
            prompt_tokens_est=prompt_tokens_est,
            has_code_or_sql=has_code_or_sql,
            schema_depth=schema_depth,
            multi_step_reasoning=multi_step_reasoning,
            domain_difficulty=domain_difficulty,
        ),
        evaluators=[EvalRule(eval_type=EvalType.EXACT_MATCH, expected_text="ok")],
    )


def test_complexity_bucket_boundaries() -> None:
    assert bucket_for_score(0.0) == ComplexityBucket.LOW
    assert bucket_for_score(0.299999) == ComplexityBucket.LOW
    assert bucket_for_score(0.30) == ComplexityBucket.MEDIUM
    assert bucket_for_score(0.699999) == ComplexityBucket.MEDIUM
    assert bucket_for_score(0.70) == ComplexityBucket.HIGH
    assert bucket_for_score(1.0) == ComplexityBucket.HIGH


def test_arm_stats_properties() -> None:
    empty = ArmStats()
    assert empty.pass_rate == 0.0
    assert empty.mean_cost_usd == Decimal("0.00000000")

    populated = ArmStats(
        pulls=4,
        passes=3,
        total_cost_usd=Decimal("0.00040000"),
        total_reward=2.994,
    )
    assert populated.pass_rate == 0.75
    assert populated.mean_cost_usd == Decimal("0.00010000")


def test_cold_start_explores_all_three_tiers_per_context() -> None:
    router = ContextualBanditRouter()
    low_item = _make_item("low-1", TaskCategory.JSON_EXTRACTION, prompt_tokens_est=60, domain_difficulty=0.1)
    high_item = _make_item(
        "high-1",
        TaskCategory.REASONING,
        prompt_tokens_est=1200,
        has_code_or_sql=True,
        schema_depth=4,
        multi_step_reasoning=True,
        domain_difficulty=0.9,
    )

    seen_low: list[ModelTier] = []
    for _ in range(3):
        tier, explanation = router.select_tier(low_item)
        assert "Cold-start" in explanation
        seen_low.append(tier)
        router.update(low_item, tier, passed=True, cost_usd=Decimal("0.00010000"))

    assert set(seen_low) == set(TIER_ORDER)

    # Second context must independently explore all 3 tiers before UCB scoring.
    seen_high: list[ModelTier] = []
    for _ in range(3):
        tier, explanation = router.select_tier(high_item)
        assert "Cold-start" in explanation
        seen_high.append(tier)
        router.update(high_item, tier, passed=(tier == ModelTier.FRONTIER), cost_usd=Decimal("0.00500000"))

    assert set(seen_high) == set(TIER_ORDER)


def test_convergence_low_extraction_to_compact_and_high_reasoning_to_frontier() -> None:
    config = ContextualBanditConfig(
        exploration_c=0.35,
        cost_penalty_lambda=15.0,
        min_quality_floor=0.85,
        seed=42,
    )
    router = ContextualBanditRouter(config=config)

    low_item = _make_item(
        "extract-low",
        TaskCategory.JSON_EXTRACTION,
        prompt_tokens_est=100,
        domain_difficulty=0.1,
    )
    high_item = _make_item(
        "reason-high",
        TaskCategory.REASONING,
        prompt_tokens_est=1200,
        has_code_or_sql=True,
        schema_depth=4,
        multi_step_reasoning=True,
        domain_difficulty=0.9,
    )

    assert router.context_for_item(low_item) == (
        TaskCategory.JSON_EXTRACTION,
        ComplexityBucket.LOW,
    )
    assert router.context_for_item(high_item) == (
        TaskCategory.REASONING,
        ComplexityBucket.HIGH,
    )

    low_costs = {
        ModelTier.COMPACT: Decimal("0.00010000"),
        ModelTier.MID: Decimal("0.01000000"),
        ModelTier.FRONTIER: Decimal("0.03000000"),
    }
    high_costs = {
        ModelTier.COMPACT: Decimal("0.00020000"),
        ModelTier.MID: Decimal("0.01000000"),
        ModelTier.FRONTIER: Decimal("0.03000000"),
    }

    low_selections: list[ModelTier] = []
    high_selections: list[ModelTier] = []

    for _ in range(100):
        tier_low, _ = router.select_tier(low_item)
        low_selections.append(tier_low)
        # All tiers pass low extraction, so cheapest tier (COMPACT) yields highest reward.
        router.update(low_item, tier_low, passed=True, cost_usd=low_costs[tier_low])

        tier_high, _ = router.select_tier(high_item)
        high_selections.append(tier_high)
        # Only FRONTIER reliably passes high reasoning.
        passed_high = tier_high == ModelTier.FRONTIER
        router.update(high_item, tier_high, passed=passed_high, cost_usd=high_costs[tier_high])

    # Tail decisions should converge to COMPACT for low extraction and FRONTIER for high reasoning.
    assert low_selections[-20:].count(ModelTier.COMPACT) >= 14
    assert high_selections[-20:] == [ModelTier.FRONTIER] * 20

    thresholds = router.export_learned_thresholds()
    assert thresholds == {
        "json_extraction:low": "compact",
        "reasoning:high": "frontier",
    }


def test_quality_floor_fallback_picks_highest_pass_rate() -> None:
    router = ContextualBanditRouter(
        ContextualBanditConfig(min_quality_floor=0.90, exploration_c=0.1)
    )
    item = _make_item("med-code", TaskCategory.CODE_SQL, prompt_tokens_est=600, has_code_or_sql=True)

    # Cold-start: COMPACT 0/1, MID 1/2 (0.50), FRONTIER 1/1 -> after 2nd pull on MID
    router.update(item, ModelTier.COMPACT, passed=False, cost_usd=Decimal("0.00010000"))
    router.update(item, ModelTier.MID, passed=False, cost_usd=Decimal("0.00100000"))
    router.update(item, ModelTier.FRONTIER, passed=False, cost_usd=Decimal("0.01000000"))
    router.update(item, ModelTier.MID, passed=True, cost_usd=Decimal("0.00100000"))

    tier, explanation = router.select_tier(item)
    assert tier == ModelTier.MID
    assert "Quality-floor fallback" in explanation


def test_json_round_trip_preserves_exact_arm_stats_and_decimal_precision() -> None:
    config = ContextualBanditConfig(
        exploration_c=0.42,
        cost_penalty_lambda=12.5,
        min_quality_floor=0.88,
        seed=7,
    )
    router = ContextualBanditRouter(config=config)
    low_item = _make_item("rt-1", TaskCategory.CLASSIFICATION, prompt_tokens_est=50, domain_difficulty=0.05)

    router.update(low_item, ModelTier.COMPACT, passed=True, cost_usd=Decimal("0.00012345"))
    router.update(low_item, ModelTier.COMPACT, passed=False, cost_usd=Decimal("0.00000001"))
    router.update(low_item, ModelTier.MID, passed=True, cost_usd=Decimal("0.00123456"))
    router.update(low_item, ModelTier.FRONTIER, passed=True, cost_usd=Decimal("0.01234567"))

    raw_json = router.to_json()
    restored = ContextualBanditRouter.from_json(raw_json)

    assert restored.config == router.config
    ctx = router.context_for_item(low_item)
    for tier in TIER_ORDER:
        orig_stats = router.get_arm_stats(ctx, tier)
        restored_stats = restored.get_arm_stats(ctx, tier)
        assert restored_stats == orig_stats
        assert isinstance(restored_stats.total_cost_usd, Decimal)
        assert restored_stats.total_cost_usd == orig_stats.total_cost_usd
        assert restored_stats.mean_cost_usd == orig_stats.mean_cost_usd

    assert restored.export_learned_thresholds() == router.export_learned_thresholds()
    assert restored.select_tier(low_item) == router.select_tier(low_item)
