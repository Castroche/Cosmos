from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class EntryProfile(BaseModel):
    """Immutable timeframe roles used by deterministic trading scans."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    base_timeframe: str
    trigger_timeframe: str
    entry_timeframe: str
    local_context_timeframe: str
    direction_context_timeframe: str
    macro_context_timeframe: str
    version: Literal[1] = 1

    @property
    def context_timeframes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.base_timeframe,
                    self.trigger_timeframe,
                    self.entry_timeframe,
                    self.local_context_timeframe,
                    self.direction_context_timeframe,
                    self.macro_context_timeframe,
                )
            )
        )


BALANCED_5M_PROFILE = EntryProfile(
    id="balanced_5m_v1",
    label="5m 平衡档",
    base_timeframe="1m",
    trigger_timeframe="1m",
    entry_timeframe="5m",
    local_context_timeframe="15m",
    direction_context_timeframe="1h",
    macro_context_timeframe="4h",
)

ENTRY_PROFILES = {BALANCED_5M_PROFILE.id: BALANCED_5M_PROFILE}
DEFAULT_ENTRY_PROFILE_ID = BALANCED_5M_PROFILE.id


def get_entry_profile(profile_id: str) -> EntryProfile:
    try:
        return ENTRY_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unsupported entry profile: {profile_id}") from exc
