from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.judge import HoldoutGeneralizationReport


def test_build_parser_audit_holdout_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["audit-holdout"])
    assert args.command == "audit-holdout"
    assert args.out == Path("./artifacts/holdout-audit")
    assert args.holdout_fraction == pytest.approx(0.30)
    assert args.quality_floor == pytest.approx(0.90)


def test_cli_audit_holdout_e2e(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "holdout_out"
    rc = main(["audit-holdout", "--out", str(out_dir)])
    assert rc == 0

    report_path = out_dir / "holdout-generalization.json"
    assert report_path.is_file()

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    report = HoldoutGeneralizationReport.model_validate(payload)
    assert report.train_requests + report.holdout_requests == 60
    assert report.train_requests >= 1
    assert report.holdout_requests >= 1
    assert report.quality_floor == pytest.approx(0.90)
    assert isinstance(report.holds_quality_floor_out_of_sample, bool)

    captured = capsys.readouterr()
    assert "train=" in captured.out
    assert "holdout=" in captured.out
    assert "Savings vs Baseline:" in captured.out
    assert f"holds_quality_floor_out_of_sample={report.holds_quality_floor_out_of_sample}" in captured.out


def test_cli_audit_holdout_custom_flags(tmp_path: Path) -> None:
    out_dir = tmp_path / "holdout_custom"
    rc = main(
        [
            "audit-holdout",
            "--out",
            str(out_dir),
            "--holdout-fraction",
            "0.25",
            "--quality-floor",
            "0.85",
        ]
    )
    assert rc == 0

    report = HoldoutGeneralizationReport.model_validate(
        json.loads((out_dir / "holdout-generalization.json").read_text(encoding="utf-8"))
    )
    assert report.train_requests + report.holdout_requests == 60
    assert report.train_requests >= 1
    assert report.holdout_requests >= 1
    assert report.quality_floor == pytest.approx(0.85)


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--holdout-fraction", "1.5"],
        ["--holdout-fraction", "0.0"],
        ["--quality-floor", "-0.1"],
        ["--quality-floor", "1.2"],
    ],
)
def test_cli_audit_holdout_invalid_flags_return_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
) -> None:
    out_dir = tmp_path / "holdout_invalid"
    rc = main(["audit-holdout", "--out", str(out_dir), *extra_args])
    assert rc == 1

    captured = capsys.readouterr()
    assert "Holdout audit failed:" in captured.err
    assert not (out_dir / "holdout-generalization.json").exists()
