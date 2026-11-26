from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    load_bundled_catalog,
)
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
)
from cost_aware_model_router.policies import default_policy_suite


def _default_gateway() -> OpenAIRouterGateway:
    policy = next(p for p in default_policy_suite() if p.policy_id == "threshold-balanced")
    return OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=policy,
    )


def test_chat_completion_request_accepts_openai_interop_params_stop_str() -> None:
    req = ChatCompletionRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="Summarize the quarterly report.")],
        top_p=0.9,
        stop="END",
        user="user-123",
        seed=42,
    )
    assert req.top_p == 0.9
    assert req.stop == "END"
    assert req.user == "user-123"
    assert req.seed == 42

    gateway = _default_gateway()
    resp = asyncio.run(gateway.handle_chat_completion(req))
    assert resp.object == "chat.completion"
    assert len(resp.choices) == 1


def test_chat_completion_request_accepts_openai_interop_params_stop_list() -> None:
    req = ChatCompletionRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="List three items.")],
        top_p=1.0,
        stop=["\n\n", "DONE"],
        user="org-user-456",
        seed=0,
    )
    assert req.top_p == 1.0
    assert req.stop == ["\n\n", "DONE"]
    assert req.user == "org-user-456"
    assert req.seed == 0

    gateway = _default_gateway()
    resp = asyncio.run(gateway.handle_chat_completion(req))
    assert resp.object == "chat.completion"
    assert len(resp.choices) == 1


def test_chat_completion_request_rejects_invalid_top_p() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="Hello")],
            top_p=1.5,
        )


def test_chat_completion_request_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "Hello"}],
                "spoofed_field": "malicious",
            }
        )
