from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import cost_aware_model_router as router_sdk
from cost_aware_model_router import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    cli,
    create_router_http_server,
    render_prometheus_metrics,
)
from cost_aware_model_router.adapters import AnthropicAdapter as DirectAnthropicAdapter
from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter as DirectDeterministicFixtureAdapter,
)
from cost_aware_model_router.adapters import (
    OpenAICompatibleAdapter as DirectOpenAICompatibleAdapter,
)
from cost_aware_model_router.adapters import ProviderAdapter as DirectProviderAdapter
from cost_aware_model_router.metrics import (
    render_prometheus_metrics as direct_render_prometheus_metrics,
)
from cost_aware_model_router.server import (
    create_router_http_server as direct_create_router_http_server,
)


def _write_minimal_workload(path: Path) -> Path:
    row = {
        "request_id": "prov-001",
        "org_id": "org_test",
        "agent_id": "agent_test",
        "category": "classification",
        "prompt": "Classify ticket #1",
        "features": {
            "prompt_tokens_est": 80,
            "has_code_or_sql": False,
            "schema_depth": 0,
            "multi_step_reasoning": False,
            "domain_difficulty": 0.1,
        },
        "evaluators": [
            {
                "eval_type": "exact_match",
                "allowed_values": ["BILLING", "TECHNICAL", "ACCOUNT"],
            }
        ],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def test_public_sdk_exports_and_sorted_all() -> None:
    expected_exports = {
        "AnthropicAdapter": DirectAnthropicAdapter,
        "DeterministicFixtureAdapter": DirectDeterministicFixtureAdapter,
        "OpenAICompatibleAdapter": DirectOpenAICompatibleAdapter,
        "ProviderAdapter": DirectProviderAdapter,
        "create_router_http_server": direct_create_router_http_server,
        "render_prometheus_metrics": direct_render_prometheus_metrics,
    }
    imported_exports = {
        "AnthropicAdapter": AnthropicAdapter,
        "DeterministicFixtureAdapter": DeterministicFixtureAdapter,
        "OpenAICompatibleAdapter": OpenAICompatibleAdapter,
        "ProviderAdapter": ProviderAdapter,
        "create_router_http_server": create_router_http_server,
        "render_prometheus_metrics": render_prometheus_metrics,
    }

    assert router_sdk.__all__ == sorted(router_sdk.__all__)
    for name, expected_obj in expected_exports.items():
        assert name in router_sdk.__all__
        assert getattr(router_sdk, name) is expected_obj
        assert imported_exports[name] is expected_obj


def test_replay_parser_provider_flag_defaults_and_choices(tmp_path: Path) -> None:
    parser = cli.build_parser()
    workload_path = tmp_path / "w.jsonl"
    out_dir = tmp_path / "out"

    default_args = parser.parse_args(
        ["replay", "--workload", str(workload_path), "--out", str(out_dir)]
    )
    assert default_args.provider == "openai"
    assert default_args.base_url == "https://api.openai.com/v1"
    assert default_args.api_key_env == "OPENAI_API_KEY"

    anthropic_args = parser.parse_args(
        [
            "replay",
            "--workload",
            str(workload_path),
            "--out",
            str(out_dir),
            "--live",
            "--provider",
            "anthropic",
        ]
    )
    assert anthropic_args.provider == "anthropic"
    assert anthropic_args.live is True

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "replay",
                "--workload",
                str(workload_path),
                "--out",
                str(out_dir),
                "--provider",
                "unknown",
            ]
        )


def test_replay_live_anthropic_uses_anthropic_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload_file = _write_minimal_workload(tmp_path / "workload.jsonl")
    out_dir = tmp_path / "out"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-test")

    captured_init: dict[str, Any] = {}
    captured_pipeline: dict[str, Any] = {}

    class DummyAnthropicAdapter:
        def __init__(self, **kwargs: Any) -> None:
            captured_init.update(kwargs)

    async def fake_run_pipeline(**kwargs: Any) -> object:
        captured_pipeline.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "AnthropicAdapter", DummyAnthropicAdapter)
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(cli, "_print_receipt_summary", lambda receipt, out: None)

    rc = cli.main(
        [
            "replay",
            "--workload",
            str(workload_file),
            "--out",
            str(out_dir),
            "--live",
            "--provider",
            "anthropic",
            "--concurrency",
            "4",
            "--max-cost-usd",
            "2.50",
        ]
    )

    assert rc == 0
    assert captured_init == {
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-env-test",
        "max_concurrency": 4,
        "max_cost_usd": Decimal("2.50"),
    }
    assert isinstance(captured_pipeline["adapter"], DummyAnthropicAdapter)


def test_replay_live_anthropic_respects_explicit_base_url_and_api_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload_file = _write_minimal_workload(tmp_path / "workload.jsonl")
    out_dir = tmp_path / "out"
    monkeypatch.setenv("CUSTOM_ANTHROPIC_KEY", "sk-ant-custom")

    captured_init: dict[str, Any] = {}

    class DummyAnthropicAdapter:
        def __init__(self, **kwargs: Any) -> None:
            captured_init.update(kwargs)

    async def fake_run_pipeline(**kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(cli, "AnthropicAdapter", DummyAnthropicAdapter)
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(cli, "_print_receipt_summary", lambda receipt, out: None)

    rc = cli.main(
        [
            "replay",
            "--workload",
            str(workload_file),
            "--out",
            str(out_dir),
            "--live",
            "--provider",
            "anthropic",
            "--base-url",
            "https://anthropic-proxy.example.com",
            "--api-key-env",
            "CUSTOM_ANTHROPIC_KEY",
        ]
    )

    assert rc == 0
    assert captured_init["base_url"] == "https://anthropic-proxy.example.com"
    assert captured_init["api_key"] == "sk-ant-custom"
