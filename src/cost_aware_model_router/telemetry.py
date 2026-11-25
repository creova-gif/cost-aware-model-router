from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_aware_model_router.schemas import (
    ModelTier,
    PolicySpec,
    ProvenanceTag,
    WorkloadItem,
    format_usd_str,
)

HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
HEX_16_RE = re.compile(r"^[0-9a-f]{16}$")

OTelAttributeValue = str | int | float | bool


def generate_trace_id(seed: str | None = None) -> str:
    """Return a 32-char lowercase hex W3C trace_id (deterministic if seed given)."""
    if seed is not None:
        return hashlib.sha256(seed.encode()).hexdigest()[:32]
    return uuid.uuid4().hex


def generate_span_id(seed: str | None = None) -> str:
    """Return a 16-char lowercase hex W3C span_id (deterministic if seed given)."""
    if seed is not None:
        return hashlib.sha256(f"span:{seed}".encode()).hexdigest()[:16]
    return uuid.uuid4().hex[:16]


class OTelSpanRecord(BaseModel):
    """OpenTelemetry GenAI & Cost-Router compatible span record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=32, max_length=32)
    span_id: str = Field(min_length=16, max_length=16)
    name: Literal["gen_ai.router.completion"] = "gen_ai.router.completion"
    start_time_unix_nano: int = Field(ge=0)
    end_time_unix_nano: int = Field(ge=0)
    attributes: dict[str, OTelAttributeValue] = Field(default_factory=dict)

    @field_validator("trace_id")
    @classmethod
    def _validate_trace_id(cls, value: str) -> str:
        normalized = value.lower()
        if not HEX_32_RE.match(normalized) or normalized == "0" * 32:
            raise ValueError("trace_id must be a non-zero 32-character lowercase hex string")
        return normalized

    @field_validator("span_id")
    @classmethod
    def _validate_span_id(cls, value: str) -> str:
        normalized = value.lower()
        if not HEX_16_RE.match(normalized) or normalized == "0" * 16:
            raise ValueError("span_id must be a non-zero 16-character lowercase hex string")
        return normalized

    @classmethod
    def create_router_span(
        cls,
        *,
        system: str,
        request_model: str,
        input_tokens: int,
        output_tokens: int,
        org_id: str,
        agent_id: str,
        policy_id: str,
        selected_tier: ModelTier,
        cost_usd: Decimal,
        cost_provenance: ProvenanceTag,
        shadow_mode: bool,
        start_time_unix_nano: int | None = None,
        end_time_unix_nano: int | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        extra_attributes: dict[str, OTelAttributeValue] | None = None,
    ) -> OTelSpanRecord:
        now_ns = time.time_ns()
        start_ns = start_time_unix_nano if start_time_unix_nano is not None else now_ns
        end_ns = end_time_unix_nano if end_time_unix_nano is not None else max(start_ns, now_ns)
        attrs: dict[str, OTelAttributeValue] = {
            "gen_ai.system": system,
            "gen_ai.request.model": request_model,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "cost_router.org_id": org_id,
            "cost_router.agent_id": agent_id,
            "cost_router.policy_id": policy_id,
            "cost_router.selected_tier": selected_tier.value,
            "cost_router.cost_usd": format_usd_str(cost_usd),
            "cost_router.cost_provenance": cost_provenance.value,
            "cost_router.shadow_mode": shadow_mode,
        }
        if extra_attributes:
            for k, v in extra_attributes.items():
                attrs[k] = v
        return cls(
            trace_id=trace_id or generate_trace_id(),
            span_id=span_id or generate_span_id(),
            name="gen_ai.router.completion",
            start_time_unix_nano=start_ns,
            end_time_unix_nano=end_ns,
            attributes=attrs,
        )


class InMemorySpanExporter:
    """Thread-safe and async-safe collector of OTelSpanRecord objects with JSONL export."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[OTelSpanRecord] = []

    def record(self, span: OTelSpanRecord) -> None:
        with self._lock:
            self._spans.append(span)

    def get_spans(self) -> list[OTelSpanRecord]:
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()

    def export_jsonl(self, path: Path) -> None:
        spans = self.get_spans()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(span.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            for span in spans
        ]
        payload = ("\n".join(lines) + "\n") if lines else ""
        path.write_text(payload, encoding="utf-8")


def load_policies_from_json(path: Path) -> list[PolicySpec]:
    """Load and validate a list of PolicySpec objects from a JSON file."""
    raw_text = path.read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    if isinstance(parsed, dict) and "policies" in parsed:
        raw_list = parsed["policies"]
    elif isinstance(parsed, list):
        raw_list = parsed
    else:
        raise ValueError("Policy JSON file must contain a JSON array or an object with 'policies'")

    if not isinstance(raw_list, list) or not raw_list:
        raise ValueError("Policy JSON must contain at least one PolicySpec")

    specs: list[PolicySpec] = []
    seen_ids: set[str] = set()
    for entry in raw_list:
        spec = PolicySpec.model_validate(entry)
        if spec.policy_id in seen_ids:
            raise ValueError(f"Duplicate policy_id in JSON file: {spec.policy_id!r}")
        seen_ids.add(spec.policy_id)
        specs.append(spec)
    return specs


def split_workload_holdout(
    workload: list[WorkloadItem], holdout_fraction: float = 0.30
) -> tuple[list[WorkloadItem], list[WorkloadItem]]:
    """
    Deterministic stratified/hashed split of a workload into (train_items, holdout_items)
    so that in-sample-fitted threshold policies can be audited out-of-sample.
    """
    if not (0.0 < holdout_fraction < 1.0):
        raise ValueError("holdout_fraction must be strictly between 0.0 and 1.0")
    if len(workload) < 2:
        raise ValueError("workload must contain at least 2 items to split into train and holdout")

    by_category: dict[str, list[WorkloadItem]] = {}
    for item in workload:
        by_category.setdefault(item.category.value, []).append(item)

    train: list[WorkloadItem] = []
    holdout: list[WorkloadItem] = []

    for cat_key in sorted(by_category.keys()):
        group = sorted(
            by_category[cat_key],
            key=lambda it: hashlib.sha256(it.request_id.encode("utf-8")).hexdigest(),
        )
        n_holdout = max(1, round(len(group) * holdout_fraction)) if len(group) > 1 else 0
        holdout_ids = {it.request_id for it in group[:n_holdout]}
        for it in by_category[cat_key]:
            if it.request_id in holdout_ids:
                holdout.append(it)
            else:
                train.append(it)

    if not holdout:
        holdout.append(train.pop())
    elif not train:
        train.append(holdout.pop())

    order_index = {it.request_id: idx for idx, it in enumerate(workload)}
    train.sort(key=lambda it: order_index[it.request_id])
    holdout.sort(key=lambda it: order_index[it.request_id])
    return train, holdout
