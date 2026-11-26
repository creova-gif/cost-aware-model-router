from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections.abc import Callable, Container
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenantRole(StrEnum):
    ADMIN = "admin"
    ROUTER_CLIENT = "router_client"
    AUDITOR = "auditor"


_ROLE_CAPABILITIES: dict[TenantRole, frozenset[TenantRole]] = {
    TenantRole.ADMIN: frozenset(
        {TenantRole.ADMIN, TenantRole.ROUTER_CLIENT, TenantRole.AUDITOR}
    ),
    TenantRole.ROUTER_CLIENT: frozenset({TenantRole.ROUTER_CLIENT}),
    TenantRole.AUDITOR: frozenset({TenantRole.AUDITOR}),
}


class TenantKeyRecord(BaseModel):
    """Non-secret metadata and SHA-256 digest for a tenant virtual API key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    role: TenantRole
    key_prefix: str = Field(min_length=1)
    key_sha256: str = Field(min_length=64, max_length=64)
    rate_limit_rps: float = Field(default=20.0, gt=0.0)
    burst_capacity: int = Field(default=40, ge=1)
    enabled: bool = True

    @field_validator("rate_limit_rps")
    @classmethod
    def _validate_finite_rps(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("rate_limit_rps must be finite")
        return value


class AuthDecision(BaseModel):
    """Structured outcome of authentication, RBAC, tenant isolation, and rate limiting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authenticated: bool
    authorized: bool
    rate_limited: bool
    org_id: str | None
    role: TenantRole | None
    status_code: Literal[200, 401, 403, 429]
    reason: str


class TokenBucketRateLimiter:
    """Thread-safe per-org_id token-bucket rate limiter."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, org_id: str, rps: float, burst: int) -> bool:
        """Consume 1 token for org_id if available, refilling at rps up to burst."""
        if not org_id:
            raise ValueError("org_id must be non-empty")
        if rps <= 0.0 or not math.isfinite(rps):
            raise ValueError("rps must be positive and finite")
        if burst < 1:
            raise ValueError("burst must be >= 1")

        with self._lock:
            now = self._clock()
            state = self._buckets.get(org_id)
            if state is None:
                tokens = float(burst)
            else:
                prev_tokens, last_refill = state
                elapsed = max(0.0, now - last_refill)
                tokens = min(float(burst), prev_tokens + elapsed * rps)

            if tokens >= 1.0:
                self._buckets[org_id] = (tokens - 1.0, now)
                return True

            self._buckets[org_id] = (tokens, now)
            return False


class TenantAuthRegistry:
    """Registry for tenant virtual API keys, RBAC enforcement, and rate limiting."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._clock = clock
        self._rate_limiter = (
            rate_limiter if rate_limiter is not None else TokenBucketRateLimiter(clock=clock)
        )
        self._lock = threading.Lock()
        self._records_by_id: dict[str, TenantKeyRecord] = {}

    def issue_key(
        self,
        org_id: str,
        role: TenantRole,
        *,
        raw_secret: str | None = None,
        rate_limit_rps: float = 20.0,
        burst_capacity: int = 40,
    ) -> tuple[str, TenantKeyRecord]:
        """Issue a new virtual API key and store only its SHA-256 digest."""
        if not org_id.strip():
            raise ValueError("org_id must be non-empty")

        with self._lock:
            raw_token, record = _build_token_and_record(
                org_id=org_id,
                role=role,
                raw_secret=raw_secret,
                rate_limit_rps=rate_limit_rps,
                burst_capacity=burst_capacity,
                existing_ids=self._records_by_id,
            )
            self._records_by_id[record.key_id] = record

        return raw_token, record

    def rotate_key(self, key_id: str) -> tuple[str, TenantKeyRecord]:
        """Atomically disable an existing key and issue a replacement with identical metadata."""
        with self._lock:
            existing = self._records_by_id.get(key_id)
            if existing is None:
                raise KeyError(f"Unknown key_id: {key_id}")
            self._records_by_id[key_id] = existing.model_copy(update={"enabled": False})
            raw_token, new_record = _build_token_and_record(
                org_id=existing.org_id,
                role=existing.role,
                rate_limit_rps=existing.rate_limit_rps,
                burst_capacity=existing.burst_capacity,
                existing_ids=self._records_by_id,
            )
            self._records_by_id[new_record.key_id] = new_record
            return raw_token, new_record

    def set_key_enabled(self, key_id: str, enabled: bool) -> TenantKeyRecord:
        """Enable or disable an existing key by key_id."""
        with self._lock:
            existing = self._records_by_id.get(key_id)
            if existing is None:
                raise KeyError(f"Unknown key_id: {key_id}")
            updated = existing.model_copy(update={"enabled": enabled})
            self._records_by_id[key_id] = updated
            return updated

    def disable_key(self, key_id: str) -> TenantKeyRecord:
        """Disable an existing key by key_id."""
        return self.set_key_enabled(key_id, False)

    def list_records(self) -> list[TenantKeyRecord]:
        """Return a thread-safe snapshot of key records sorted by key_id."""
        with self._lock:
            return sorted(self._records_by_id.values(), key=lambda rec: rec.key_id)

    def to_json(self) -> str:
        """Serialize non-secret key records (with SHA-256 digests) to JSON."""
        payload = {"records": [rec.model_dump(mode="json") for rec in self.list_records()]}
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(
        cls,
        raw_json: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> TenantAuthRegistry:
        """Load a TenantAuthRegistry from a JSON string produced by to_json()."""
        payload = json.loads(raw_json)
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError(
                "Invalid TenantAuthRegistry JSON: expected an object with a 'records' list"
            )
        registry = cls(clock=clock, rate_limiter=rate_limiter)
        for item in payload["records"]:
            record = TenantKeyRecord.model_validate(item)
            if record.key_id in registry._records_by_id:
                raise ValueError(f"Duplicate key_id in auth config: {record.key_id}")
            registry._records_by_id[record.key_id] = record
        return registry

    def authenticate_and_authorize(
        self,
        authorization_header: str | None,
        *,
        required_role: TenantRole,
        claimed_org_id: str | None = None,
    ) -> AuthDecision:
        """Verify Bearer key, tenant isolation, RBAC role, and per-tenant rate limit."""
        token = _extract_bearer_token(authorization_header)
        if token is None:
            return AuthDecision(
                authenticated=False,
                authorized=False,
                rate_limited=False,
                org_id=None,
                role=None,
                status_code=401,
                reason="Missing or malformed Bearer authorization header",
            )

        presented_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            records_snapshot = tuple(self._records_by_id.values())

        matched_record: TenantKeyRecord | None = None
        for record in records_snapshot:
            if hmac.compare_digest(record.key_sha256, presented_sha256):
                matched_record = record

        if matched_record is None:
            return AuthDecision(
                authenticated=False,
                authorized=False,
                rate_limited=False,
                org_id=None,
                role=None,
                status_code=401,
                reason="Invalid API key",
            )

        if not matched_record.enabled:
            return AuthDecision(
                authenticated=True,
                authorized=False,
                rate_limited=False,
                org_id=matched_record.org_id,
                role=matched_record.role,
                status_code=403,
                reason="API key is disabled",
            )

        if claimed_org_id is not None and claimed_org_id != matched_record.org_id:
            return AuthDecision(
                authenticated=True,
                authorized=False,
                rate_limited=False,
                org_id=matched_record.org_id,
                role=matched_record.role,
                status_code=403,
                reason="Cross-tenant org_id spoofing rejected",
            )

        if required_role not in _ROLE_CAPABILITIES[matched_record.role]:
            return AuthDecision(
                authenticated=True,
                authorized=False,
                rate_limited=False,
                org_id=matched_record.org_id,
                role=matched_record.role,
                status_code=403,
                reason=(
                    f"Role '{matched_record.role.value}' lacks required "
                    f"permission '{required_role.value}'"
                ),
            )

        if not self._rate_limiter.allow(
            matched_record.org_id,
            matched_record.rate_limit_rps,
            matched_record.burst_capacity,
        ):
            return AuthDecision(
                authenticated=True,
                authorized=True,
                rate_limited=True,
                org_id=matched_record.org_id,
                role=matched_record.role,
                status_code=429,
                reason="Tenant rate limit exceeded",
            )

        return AuthDecision(
            authenticated=True,
            authorized=True,
            rate_limited=False,
            org_id=matched_record.org_id,
            role=matched_record.role,
            status_code=200,
            reason="Authorized",
        )


def _build_token_and_record(
    *,
    org_id: str,
    role: TenantRole,
    raw_secret: str | None = None,
    rate_limit_rps: float = 20.0,
    burst_capacity: int = 40,
    existing_ids: Container[str] = (),
) -> tuple[str, TenantKeyRecord]:
    key_id = secrets.token_hex(6)
    while key_id in existing_ids:
        key_id = secrets.token_hex(6)

    if raw_secret is None:
        secret_part = secrets.token_urlsafe(24)
        raw_token = f"crk_{key_id}.{secret_part}"
        key_prefix = f"crk_{key_id}"
    else:
        if not raw_secret.strip():
            raise ValueError("raw_secret must be non-empty")
        raw_token = raw_secret
        if raw_token.startswith("crk_") and "." in raw_token:
            key_prefix = raw_token.split(".", 1)[0]
        else:
            key_prefix = raw_token[: min(8, len(raw_token))]

    key_sha256 = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    record = TenantKeyRecord(
        key_id=key_id,
        org_id=org_id,
        role=role,
        key_prefix=key_prefix,
        key_sha256=key_sha256,
        rate_limit_rps=rate_limit_rps,
        burst_capacity=burst_capacity,
        enabled=True,
    )
    return raw_token, record


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if authorization_header is None:
        return None
    stripped = authorization_header.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token or any(ch.isspace() for ch in token):
        return None
    return token
