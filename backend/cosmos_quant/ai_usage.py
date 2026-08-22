from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field

from .ai_configuration import AIProvider, AIReasoningEffort


class AIUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_hit_input_tokens: int = Field(default=0, ge=0)
    cache_miss_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    visible_output_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: Decimal | None = Field(default=None, ge=0)
    pricing_tier: str | None = None

    @computed_field
    @property
    def cache_hit_rate(self) -> float:
        cacheable = self.cache_hit_input_tokens + self.cache_miss_input_tokens
        return self.cache_hit_input_tokens / cacheable if cacheable else 0.0


class AICompletionResult(BaseModel):
    provider: AIProvider
    model: str
    data: dict
    usage: AIUsage = Field(default_factory=AIUsage)
    latency_ms: int = Field(ge=0)
    provider_request_id: str | None = None
    operation: str = "structured_generation"
    thinking_enabled: bool | None = None
    reasoning_effort: AIReasoningEffort | None = None
    prompt_version: str = "v1"
    schema_version: str = "v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AIUsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: f"aiuse_{uuid4().hex[:20]}")
    provider_request_id: str | None = None
    provider: AIProvider
    model: str
    operation: str = Field(min_length=2, max_length=100)
    thinking_enabled: bool | None = None
    reasoning_effort: AIReasoningEffort | None = None
    prompt_version: str = Field(default="v1", min_length=1, max_length=100)
    schema_version: str = Field(default="v1", min_length=1, max_length=100)
    usage: AIUsage
    latency_ms: int = Field(ge=0)
    created_at: datetime


class AIUsageSummary(BaseModel):
    since: datetime
    until: datetime
    request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_hit_input_tokens: int = Field(default=0, ge=0)
    cache_miss_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    visible_output_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: Decimal = Field(default=Decimal("0"), ge=0)
    unpriced_request_count: int = Field(default=0, ge=0)

    @computed_field
    @property
    def cache_hit_rate(self) -> float:
        cacheable = self.cache_hit_input_tokens + self.cache_miss_input_tokens
        return self.cache_hit_input_tokens / cacheable if cacheable else 0.0


class AIUsageReport(BaseModel):
    summary: AIUsageSummary
    recent: list[AIUsageEvent] = Field(default_factory=list)


@dataclass(frozen=True)
class DeepSeekPrice:
    cache_hit_input: Decimal
    cache_miss_input: Decimal
    output: Decimal
    tier: Literal["legacy", "off_peak", "peak"]


_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
_NEW_PRICING_START = datetime(2026, 8, 17, tzinfo=_SHANGHAI)
_MILLION = Decimal(1_000_000)


def deepseek_price(model: str, at: datetime) -> DeepSeekPrice | None:
    normalized = model.lower()
    if "deepseek-v4-flash" in normalized:
        family = "flash"
    elif "deepseek-v4-pro" in normalized:
        family = "pro"
    else:
        return None

    local = at.astimezone(_SHANGHAI)
    if local < _NEW_PRICING_START:
        if family == "flash":
            return DeepSeekPrice(Decimal("0.02"), Decimal("1"), Decimal("2"), "legacy")
        return DeepSeekPrice(Decimal("0.025"), Decimal("3"), Decimal("6"), "legacy")

    peak = 9 <= local.hour < 12 or 14 <= local.hour < 18
    if family == "flash":
        return (
            DeepSeekPrice(Decimal("0.10"), Decimal("3.0"), Decimal("9.0"), "peak")
            if peak
            else DeepSeekPrice(
                Decimal("0.05"), Decimal("1.5"), Decimal("4.5"), "off_peak"
            )
        )
    return (
        DeepSeekPrice(Decimal("0.30"), Decimal("9.0"), Decimal("27.0"), "peak")
        if peak
        else DeepSeekPrice(Decimal("0.15"), Decimal("4.5"), Decimal("13.5"), "off_peak")
    )


def estimate_deepseek_cost_cny(
    *,
    model: str,
    at: datetime,
    cache_hit_input_tokens: int,
    cache_miss_input_tokens: int,
    output_tokens: int,
) -> tuple[Decimal | None, str | None]:
    price = deepseek_price(model, at)
    if price is None:
        return None, None
    cost = (
        Decimal(cache_hit_input_tokens) * price.cache_hit_input
        + Decimal(cache_miss_input_tokens) * price.cache_miss_input
        + Decimal(output_tokens) * price.output
    ) / _MILLION
    return cost.quantize(Decimal("0.00000001")), price.tier
