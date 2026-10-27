from cosmos_quant.entry_profiles import BALANCED_5M_PROFILE, get_entry_profile


def test_balanced_5m_profile_assigns_entry_and_context_roles():
    profile = get_entry_profile("balanced_5m_v1")

    assert profile is BALANCED_5M_PROFILE
    assert profile.base_timeframe == "1m"
    assert profile.trigger_timeframe == "1m"
    assert profile.entry_timeframe == "5m"
    assert profile.local_context_timeframe == "15m"
    assert profile.direction_context_timeframe == "1h"
    assert profile.macro_context_timeframe == "4h"
    assert profile.context_timeframes == ("1m", "5m", "15m", "1h", "4h")
