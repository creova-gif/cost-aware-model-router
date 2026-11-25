from __future__ import annotations

from decimal import Decimal

import pytest

from cost_aware_model_router.schemas import (
    ComplexityFeatures,
    MetricProvenance,
    ModelTier,
    ProvenanceTag,
    TierModelSpec,
    format_usd_str,
    quantize_usd,
)


def test_quantize_usd_8_places_and_rejects_non_finite() -> None:
    assert quantize_usd("0.1") + quantize_usd("0.2") == Decimal("0.30000000")
    assert format_usd_str(quantize_usd("0.000000005")) == "0.00000001"
    with pytest.raises(ValueError, match="finite"):
        quantize_usd(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        quantize_usd(float("inf"))


def test_tier_model_spec_cost_calculation_and_validation() -> None:
    spec = TierModelSpec(
        tier=ModelTier.COMPACT,
        model_id="test-compact",
        provider="openai_compatible",
        input_usd_per_1m=Decimal("0.15"),
        output_usd_per_1m=Decimal("0.60"),
        price_source="unit-test",
    )
    # 1000 input tokens = 0.00015000, 500 output tokens = 0.00030000 -> total 0.00045000
    assert spec.compute_cost_usd(1000, 500) == Decimal("0.00045000")

    with pytest.raises(ValueError, match="negative"):
        TierModelSpec(
            tier=ModelTier.COMPACT,
            model_id="bad",
            provider="openai_compatible",
            input_usd_per_1m=Decimal("-1.0"),
            output_usd_per_1m=Decimal("0.60"),
            price_source="unit-test",
        )


def test_metric_provenance_summary_tag() -> None:
    measured = MetricProvenance(
        tokens_provenance=ProvenanceTag.MEASURED,
        latency_provenance=ProvenanceTag.MEASURED,
        verdict_provenance=ProvenanceTag.MEASURED,
        cost_provenance=ProvenanceTag.MODELED,
    )
    assert not measured.is_any_unknown
    assert measured.summary_tag == ProvenanceTag.MEASURED

    unknown = MetricProvenance(
        tokens_provenance=ProvenanceTag.MEASURED,
        latency_provenance=ProvenanceTag.MEASURED,
        verdict_provenance=ProvenanceTag.MEASURED,
        cost_provenance=ProvenanceTag.UNKNOWN,
    )
    assert unknown.is_any_unknown
    assert unknown.summary_tag == ProvenanceTag.UNKNOWN


def test_complexity_features_weighted_score_bounds() -> None:
    low = ComplexityFeatures(prompt_tokens_est=60, domain_difficulty=0.05)
    high = ComplexityFeatures(
        prompt_tokens_est=2400,
        has_code_or_sql=True,
        schema_depth=5,
        multi_step_reasoning=True,
        domain_difficulty=1.0,
    )
    assert 0.0 <= low.weighted_score() < 0.20
    assert high.weighted_score() == 1.0
