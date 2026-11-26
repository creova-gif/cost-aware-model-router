from __future__ import annotations

from pathlib import Path

import pytest

from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.learner import ContextualBanditRouter
from cost_aware_model_router.schemas import ModelTier


def test_build_parser_train_bandit_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["train-bandit"])
    assert args.command == "train-bandit"
    assert args.out == Path("./artifacts/bandit")
    assert args.epochs == 4
    assert args.quality_floor == pytest.approx(0.85)


def test_cli_train_bandit_e2e(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "bandit_out"
    rc = main(["train-bandit", "--epochs", "3", "--out", str(out_dir)])
    assert rc == 0

    policy_path = out_dir / "bandit-policy.json"
    assert policy_path.is_file()

    router = ContextualBanditRouter.from_json(policy_path.read_text(encoding="utf-8"))
    assert router.config.min_quality_floor == pytest.approx(0.85)

    learned = router.export_learned_thresholds()
    assert len(learned) > 0
    valid_tiers = {tier.value for tier in ModelTier}
    assert all(tier_val in valid_tiers for tier_val in learned.values())

    captured = capsys.readouterr()
    assert "Trained ContextualBanditRouter for 3 epochs" in captured.out
    assert "Final Epoch Pass Rate:" in captured.out
    assert "Learned (TaskCategory, ComplexityBucket) -> ModelTier:" in captured.out
    assert "->" in captured.out


def test_cli_train_bandit_custom_quality_floor(tmp_path: Path) -> None:
    out_dir = tmp_path / "bandit_custom"
    rc = main(
        [
            "train-bandit",
            "--epochs",
            "2",
            "--quality-floor",
            "0.90",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0

    router = ContextualBanditRouter.from_json(
        (out_dir / "bandit-policy.json").read_text(encoding="utf-8")
    )
    assert router.config.min_quality_floor == pytest.approx(0.90)


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--epochs", "0"],
        ["--epochs", "-1"],
        ["--epochs", "-5"],
        ["--quality-floor", "-0.1"],
        ["--quality-floor", "1.2"],
    ],
)
def test_cli_train_bandit_invalid_flags_return_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
) -> None:
    out_dir = tmp_path / "bandit_invalid"
    rc = main(["train-bandit", "--out", str(out_dir), *extra_args])
    assert rc == 1

    captured = capsys.readouterr()
    assert "Bandit training failed:" in captured.err
    assert not (out_dir / "bandit-policy.json").exists()
