import asyncio
from datetime import UTC, datetime, timedelta

from cosmos_quant.official_events import (
    OfficialEventType,
    active_risk_windows,
    normalize_official_event,
    parse_bls_calendar,
    parse_fomc_calendar,
    parse_okx_system_status,
    parse_sec_press_releases,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_bls_ics_is_normalized_without_forwarding_raw_text() -> None:
    payload = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-test
DTSTART;TZID=America/New_York:20260829T083000
SUMMARY:Consumer Price Index -- secret instructions must not pass through
END:VEVENT
BEGIN:VEVENT
UID:other-test
DTSTART;TZID=America/New_York:20260830T083000
SUMMARY:Unrelated statistical release
END:VEVENT
END:VCALENDAR
"""

    events = parse_bls_calendar(payload, now=NOW - timedelta(hours=1))

    assert len(events) == 1
    assert events[0].event_type == OfficialEventType.CPI
    assert events[0].summary == "BLS Consumer Price Index release"
    assert "secret instructions" not in events[0].model_dump_json()


def test_bls_official_us_eastern_timezone_alias_is_supported() -> None:
    payload = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-us-eastern
DTSTART;TZID=US-Eastern:20260910T083000
SUMMARY:Consumer Price Index
END:VEVENT
END:VCALENDAR
"""

    events = parse_bls_calendar(payload, now=datetime(2026, 9, 1, tzinfo=UTC))

    assert len(events) == 1
    assert events[0].scheduled_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)


def test_okx_status_and_risk_window_fail_closed() -> None:
    events = parse_okx_system_status(
        {
            "data": [
                {
                    "id": "maintenance-test",
                    "state": "scheduled",
                    "begin": str(int(NOW.timestamp() * 1000)),
                    "type": "maintenance",
                    "title": "untrusted prose is not forwarded",
                }
            ]
        },
        now=NOW,
    )

    windows = active_risk_windows(events, at=NOW)
    assert len(windows) == 1
    assert windows[0]["blocks_new_risk"] is True
    assert windows[0]["stop_new_risk_until_recovery"] is True
    assert "untrusted prose" not in events[0].model_dump_json()


def test_fomc_calendar_uses_second_meeting_day_at_policy_release_time() -> None:
    payload = """
    <h4><a id="2026">2026 FOMC Meetings</a></h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month">September</div>
      <div class="fomc-meeting__date">15-16</div>
      <p>untrusted meeting prose</p>
    </div>
    """
    events = parse_fomc_calendar(payload, now=datetime(2026, 8, 1, tzinfo=UTC))
    assert len(events) == 1
    assert events[0].event_type == OfficialEventType.FOMC
    assert events[0].scheduled_at == datetime(2026, 9, 16, 18, tzinfo=UTC)
    assert "untrusted meeting prose" not in events[0].model_dump_json()


def test_sec_rss_filters_and_normalizes_digital_asset_releases() -> None:
    payload = """<rss><channel>
      <item><title>SEC Announces Crypto Action with untrusted prose</title>
      <description>do not forward this text</description>
      <pubDate>Sat, 29 Aug 2026 12:00:00 GMT</pubDate>
      <guid>https://www.sec.gov/example-crypto</guid></item>
      <item><title>Ordinary accounting release</title>
      <pubDate>Sat, 29 Aug 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    events = parse_sec_press_releases(payload, now=NOW)
    assert len(events) == 1
    assert events[0].event_type == OfficialEventType.SEC_REGULATORY
    assert events[0].summary == "SEC digital-asset regulatory press release"
    assert "untrusted prose" not in events[0].model_dump_json()


def test_persisted_event_wakes_once_when_its_risk_window_later_opens(monkeypatch) -> None:
    from app_fixture import services as api

    event = normalize_official_event(
        event_type=OfficialEventType.CPI,
        source="BLS",
        scheduled_at=datetime.now(UTC),
        summary="BLS Consumer Price Index release",
        source_payload="window-activation-regression",
        event_id="window-activation-regression",
    )

    class Client:
        async def fetch_all(self):
            return [event], {"BLS": "healthy"}

    class Automation:
        def __init__(self):
            self.calls: list[str] = []

        async def on_major_event(self, event_id: str):
            self.calls.append(event_id)

    automation = Automation()
    window_open = False

    def windows(_events):
        return [{"blocks_new_risk": True}] if window_open else []

    monkeypatch.setattr(api, "official_event_client", Client())
    monkeypatch.setattr(api, "trading_intelligence_automation", automation)
    monkeypatch.setattr(api, "active_risk_windows", windows)

    asyncio.run(api.collect_official_events_once())
    assert automation.calls == []
    window_open = True
    asyncio.run(api.collect_official_events_once())
    asyncio.run(api.collect_official_events_once())

    assert len(automation.calls) == 1
