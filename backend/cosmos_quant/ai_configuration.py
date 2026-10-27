from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


class AIProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    XAI = "xai"
    DEEPSEEK = "deepseek"
    MISTRAL = "mistral"
    QWEN = "qwen"
    CUSTOM = "custom"


class AIReasoningEffort(StrEnum):
    HIGH = "high"
    MAX = "max"


PROVIDER_DEFAULTS: dict[AIProvider, tuple[str, str]] = {
    AIProvider.OPENAI: ("https://api.openai.com/v1", "gpt-5.6-terra"),
    AIProvider.ANTHROPIC: ("https://api.anthropic.com/v1", "claude-opus-4-6"),
    AIProvider.GOOGLE: (
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.5-flash",
    ),
    AIProvider.XAI: ("https://api.x.ai/v1", "grok-4.5"),
    AIProvider.DEEPSEEK: ("https://api.deepseek.com", "deepseek-flash"),
    AIProvider.MISTRAL: ("https://api.mistral.ai/v1", "mistral-large-latest"),
    AIProvider.QWEN: (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.7-plus",
    ),
    AIProvider.CUSTOM: ("https://example.com/v1", "model-id"),
}


class AIProviderConfiguration(BaseModel):
    provider: AIProvider
    enabled: bool = False
    model: str = Field(min_length=2, max_length=160)
    base_url: str = Field(min_length=8, max_length=500)
    priority: int = Field(default=100, ge=1, le=999)
    timeout_seconds: int = Field(default=45, ge=5, le=300)
    allow_preview_models: bool = False
    thinking_enabled: bool = True
    reasoning_effort: AIReasoningEffort = AIReasoningEffort.HIGH

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("model ID must be non-empty and cannot contain spaces")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AI base URL must be a valid HTTP(S) endpoint")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("external AI endpoints must use HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in the AI base URL")
        if parsed.query or parsed.fragment:
            raise ValueError("AI base URL cannot contain a query string or fragment")
        return normalized

    @model_validator(mode="after")
    def validate_provider_endpoint(self):
        hostname = (urlparse(self.base_url).hostname or "").lower()
        allowed_hosts: dict[AIProvider, tuple[str, ...]] = {
            AIProvider.OPENAI: ("api.openai.com",),
            AIProvider.ANTHROPIC: ("api.anthropic.com",),
            AIProvider.GOOGLE: ("generativelanguage.googleapis.com",),
            AIProvider.XAI: ("api.x.ai",),
            AIProvider.DEEPSEEK: ("api.deepseek.com",),
            AIProvider.MISTRAL: ("api.mistral.ai",),
        }
        if self.provider == AIProvider.QWEN:
            if hostname != "dashscope.aliyuncs.com" and not hostname.endswith(
                ".dashscope.aliyuncs.com"
            ):
                raise ValueError("Qwen API endpoint must use an official DashScope host")
        elif self.provider in allowed_hosts and hostname not in allowed_hosts[self.provider]:
            raise ValueError(f"{self.provider} API endpoint must use its official provider host")
        return self


def default_provider_configurations() -> list[AIProviderConfiguration]:
    return [
        AIProviderConfiguration(
            provider=provider,
            base_url=base_url,
            model=model,
            priority=index * 10,
        )
        for index, (provider, (base_url, model)) in enumerate(PROVIDER_DEFAULTS.items(), start=1)
    ]


class AIConfiguration(BaseModel):
    routing_mode: Literal["manual", "failover", "consensus"] = "failover"
    primary_provider: AIProvider = AIProvider.OPENAI
    daily_budget_usd: Decimal = Field(default=Decimal("10"), ge=0, le=10000)
    providers: list[AIProviderConfiguration] = Field(
        default_factory=default_provider_configurations
    )

    @model_validator(mode="after")
    def validate_provider_set(self):
        provider_ids = [item.provider for item in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("each AI provider can only appear once")
        if set(provider_ids) != set(AIProvider):
            raise ValueError("AI configuration must include every supported provider")
        return self


class AICredentialInput(BaseModel):
    provider: AIProvider
    api_key: str | None = Field(default=None, max_length=1000)
    clear_existing: bool = False

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AIConfigurationUpdate(BaseModel):
    configuration: AIConfiguration
    credential: AICredentialInput | None = None


class AIProviderCredentialState(BaseModel):
    api_key_configured: bool = False


class AIProviderRuntimeState(BaseModel):
    status: Literal["untested", "ready", "error"] = "untested"
    latency_ms: int | None = None
    tested_at: datetime | None = None
    model_count: int = 0
    models: list[str] = Field(default_factory=list)
    selected_model_available: bool | None = None
    failure_code: str | None = None
    message: str = "尚未执行连接测试"


class AIConfigurationView(BaseModel):
    configuration: AIConfiguration
    credentials: dict[AIProvider, AIProviderCredentialState]
    runtime: dict[AIProvider, AIProviderRuntimeState]
    updated_at: datetime | None = None
