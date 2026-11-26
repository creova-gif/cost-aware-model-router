from __future__ import annotations

from decimal import Decimal

import pytest

from cost_aware_model_router.adapters import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
)
from cost_aware_model_router.cli import _build_gateway_for_serve, build_parser
from cost_aware_model_router.resilience import FallbackChainAdapter


def test_serve_parser_live_provider_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "serve",
            "--live",
            "--provider",
            "anthropic",
            "--enable-fallback",
            "--max-cost-usd",
            "2.50",
        ]
    )

    assert args.command == "serve"
    assert args.live is True
    assert args.provider == "anthropic"
    assert args.enable_fallback is True
    assert args.max_cost_usd == "2.50"
    assert args.base_url == "https://api.openai.com/v1"
    assert args.api_key_env == "OPENAI_API_KEY"
    assert args.concurrency == 4


def test_serve_parser_live_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["serve"])

    assert args.live is False
    assert args.provider == "openai"
    assert args.base_url == "https://api.openai.com/v1"
    assert args.api_key_env == "OPENAI_API_KEY"
    assert args.max_cost_usd == "5.00"
    assert args.concurrency == 4


def test_build_gateway_for_serve_default_uses_deterministic_fixture() -> None:
    gateway = _build_gateway_for_serve(policy_id="threshold-balanced")
    assert isinstance(gateway.adapter, DeterministicFixtureAdapter)


def test_build_gateway_for_serve_live_openai_local() -> None:
    gateway = _build_gateway_for_serve(
        policy_id="threshold-balanced",
        live=True,
        provider="openai",
        base_url="http://localhost:11434/v1",
    )
    assert isinstance(gateway.adapter, OpenAICompatibleAdapter)


def test_build_gateway_for_serve_live_anthropic_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    gateway = _build_gateway_for_serve(
        policy_id="threshold-balanced",
        live=True,
        provider="anthropic",
        base_url="https://api.anthropic.com",
        enable_fallback=True,
        max_cost_usd=Decimal("2.50"),
        concurrency=2,
    )
    assert isinstance(gateway.adapter, FallbackChainAdapter)
    assert isinstance(gateway.adapter._inner, AnthropicAdapter)


def test_build_gateway_for_serve_rejects_plaintext_http_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    with pytest.raises(ValueError, match="plaintext http://"):
        _build_gateway_for_serve(
            policy_id="threshold-balanced",
            live=True,
            provider="openai",
            base_url="http://remote.example.com/v1",
            api_key_env="OPENAI_API_KEY",
        )
