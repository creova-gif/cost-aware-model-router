from __future__ import annotations

import hashlib
import hmac
import threading

import pytest
from pydantic import ValidationError

from cost_aware_model_router.governance import (
    AuthDecision,
    TenantAuthRegistry,
    TenantKeyRecord,
    TenantRole,
    TokenBucketRateLimiter,
)


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_issue_key_generates_crk_token_and_never_stores_plaintext_secret() -> None:
    registry = TenantAuthRegistry()
    raw_token, record = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)

    assert raw_token.startswith(f"crk_{record.key_id}.")
    assert record.key_prefix == f"crk_{record.key_id}"
    assert record.org_id == "org_alpha"
    assert record.role == TenantRole.ROUTER_CLIENT
    assert record.enabled is True
    assert record.key_sha256 == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    secret_part = raw_token.split(".", 1)[1]
    serialized = record.model_dump_json()
    assert raw_token not in serialized
    assert secret_part not in serialized


def test_issue_key_with_custom_raw_secret_stores_only_digest() -> None:
    registry = TenantAuthRegistry()
    custom_secret = "crk_custom.super_secret_value_12345"
    returned_token, record = registry.issue_key(
        "org_beta",
        TenantRole.AUDITOR,
        raw_secret=custom_secret,
        rate_limit_rps=5.0,
        burst_capacity=10,
    )

    assert returned_token == custom_secret
    assert record.key_sha256 == hashlib.sha256(custom_secret.encode("utf-8")).hexdigest()
    assert "super_secret_value_12345" not in record.model_dump_json()


def test_schemas_are_frozen_and_forbid_extra_fields() -> None:
    registry = TenantAuthRegistry()
    _, record = registry.issue_key("org_alpha", TenantRole.ADMIN)

    with pytest.raises(ValidationError):
        record.org_id = "org_other"

    with pytest.raises(ValidationError):
        TenantKeyRecord.model_validate(
            {
                "key_id": "k1",
                "org_id": "org_alpha",
                "role": TenantRole.ADMIN,
                "key_prefix": "crk_k1",
                "key_sha256": "a" * 64,
                "unexpected_field": "nope",
            }
        )

    decision = AuthDecision(
        authenticated=True,
        authorized=True,
        rate_limited=False,
        org_id="org_alpha",
        role=TenantRole.ADMIN,
        status_code=200,
        reason="Authorized",
    )
    with pytest.raises(ValidationError):
        decision.status_code = 403

    with pytest.raises(ValidationError):
        AuthDecision.model_validate(
            {
                "authenticated": False,
                "authorized": False,
                "rate_limited": False,
                "org_id": None,
                "role": None,
                "status_code": 401,
                "reason": "Bad",
                "extra_field": 123,
            }
        )


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer   ",
        "Bearer token with spaces",
        "crk_missing_bearer_prefix",
    ],
)
def test_missing_or_malformed_bearer_header_returns_401(header: str | None) -> None:
    registry = TenantAuthRegistry()
    registry.issue_key("org_alpha", TenantRole.ADMIN)

    decision = registry.authenticate_and_authorize(
        header,
        required_role=TenantRole.ADMIN,
    )
    assert decision.status_code == 401
    assert decision.authenticated is False
    assert decision.authorized is False
    assert decision.rate_limited is False
    assert decision.org_id is None
    assert decision.role is None


def test_unknown_key_returns_401_and_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TenantAuthRegistry()
    raw_token, _ = registry.issue_key("org_alpha", TenantRole.ROUTER_CLIENT)

    compare_calls: list[tuple[str, str]] = []
    real_compare = hmac.compare_digest

    def spy_compare(a: str, b: str) -> bool:
        compare_calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr("cost_aware_model_router.governance.hmac.compare_digest", spy_compare)

    bad_decision = registry.authenticate_and_authorize(
        "Bearer crk_wrong.secret",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert bad_decision.status_code == 401
    assert bad_decision.authenticated is False
    assert len(compare_calls) == 1

    good_decision = registry.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert good_decision.status_code == 200
    assert good_decision.authenticated is True
    assert len(compare_calls) == 2


@pytest.mark.parametrize(
    ("key_role", "required_role", "expected_status"),
    [
        (TenantRole.ADMIN, TenantRole.ADMIN, 200),
        (TenantRole.ADMIN, TenantRole.ROUTER_CLIENT, 200),
        (TenantRole.ADMIN, TenantRole.AUDITOR, 200),
        (TenantRole.ROUTER_CLIENT, TenantRole.ROUTER_CLIENT, 200),
        (TenantRole.ROUTER_CLIENT, TenantRole.ADMIN, 403),
        (TenantRole.ROUTER_CLIENT, TenantRole.AUDITOR, 403),
        (TenantRole.AUDITOR, TenantRole.AUDITOR, 200),
        (TenantRole.AUDITOR, TenantRole.ROUTER_CLIENT, 403),
        (TenantRole.AUDITOR, TenantRole.ADMIN, 403),
    ],
)
def test_rbac_matrix(
    key_role: TenantRole,
    required_role: TenantRole,
    expected_status: int,
) -> None:
    registry = TenantAuthRegistry()
    raw_token, _ = registry.issue_key("org_rbac", key_role)

    decision = registry.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=required_role,
    )
    assert decision.status_code == expected_status
    assert decision.authenticated is True
    assert decision.authorized is (expected_status == 200)
    assert decision.org_id == "org_rbac"
    assert decision.role == key_role


def test_cross_tenant_org_spoofing_rejected_with_403() -> None:
    registry = TenantAuthRegistry()
    raw_token, _ = registry.issue_key("org_victim", TenantRole.ADMIN)

    spoof_decision = registry.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=TenantRole.ROUTER_CLIENT,
        claimed_org_id="org_attacker",
    )
    assert spoof_decision.status_code == 403
    assert spoof_decision.authenticated is True
    assert spoof_decision.authorized is False
    assert spoof_decision.org_id == "org_victim"
    assert "spoofing" in spoof_decision.reason.lower()

    matching_decision = registry.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=TenantRole.ROUTER_CLIENT,
        claimed_org_id="org_victim",
    )
    assert matching_decision.status_code == 200
    assert matching_decision.authorized is True


def test_disabled_key_rejected_with_403() -> None:
    registry = TenantAuthRegistry()
    raw_token, record = registry.issue_key("org_alpha", TenantRole.ADMIN)

    disabled_record = registry.disable_key(record.key_id)
    assert disabled_record.enabled is False

    decision = registry.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=TenantRole.ADMIN,
    )
    assert decision.status_code == 403
    assert decision.authenticated is True
    assert decision.authorized is False
    assert "disabled" in decision.reason.lower()


def test_deterministic_token_bucket_429_and_refill() -> None:
    clock = FakeClock(initial=1000.0)
    registry = TenantAuthRegistry(clock=clock)
    token_a, _ = registry.issue_key(
        "org_alpha",
        TenantRole.ROUTER_CLIENT,
        rate_limit_rps=2.0,
        burst_capacity=3,
    )
    token_b, _ = registry.issue_key(
        "org_beta",
        TenantRole.ROUTER_CLIENT,
        rate_limit_rps=2.0,
        burst_capacity=3,
    )

    # Burst of 3 succeeds for org_alpha
    for _ in range(3):
        dec = registry.authenticate_and_authorize(
            f"Bearer {token_a}",
            required_role=TenantRole.ROUTER_CLIENT,
        )
        assert dec.status_code == 200
        assert dec.rate_limited is False

    # 4th immediate call is rate-limited (429)
    limited = registry.authenticate_and_authorize(
        f"Bearer {token_a}",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert limited.status_code == 429
    assert limited.authenticated is True
    assert limited.authorized is True
    assert limited.rate_limited is True

    # Per-tenant isolation: org_beta still has full burst capacity
    beta_dec = registry.authenticate_and_authorize(
        f"Bearer {token_b}",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert beta_dec.status_code == 200

    # Advance 0.5s at 2.0 rps -> +1.0 token refilled
    clock.advance(0.5)
    refilled = registry.authenticate_and_authorize(
        f"Bearer {token_a}",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert refilled.status_code == 200
    assert refilled.rate_limited is False

    # Immediately exhausted again
    limited_again = registry.authenticate_and_authorize(
        f"Bearer {token_a}",
        required_role=TenantRole.ROUTER_CLIENT,
    )
    assert limited_again.status_code == 429


def test_token_bucket_rate_limiter_thread_safety() -> None:
    clock = FakeClock(initial=500.0)
    limiter = TokenBucketRateLimiter(clock=clock)
    burst = 25
    total_threads = 50
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(total_threads)

    def worker() -> None:
        barrier.wait()
        allowed = limiter.allow("org_concurrent", rps=10.0, burst=burst)
        with results_lock:
            results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(total_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == total_threads
    assert sum(1 for r in results if r) == burst
