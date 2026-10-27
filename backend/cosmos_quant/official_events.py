from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Self
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

OFFICIAL_SOURCES = {
    "BLS": "https://www.bls.gov/schedule/news_release/bls.ics",
    "FEDERAL_RESERVE": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "SEC": "https://www.sec.gov/newsroom/press-releases",
    "OKX": "https://www.okx.com/docs-v5/trick_en/",
}
SEC_RSS_URL = "https://www.sec.gov/news/pressreleases.rss"


class OfficialEventType(StrEnum):
    CPI = "CPI"
    NFP = "NFP"
    FOMC = "FOMC"
    SEC_REGULATORY = "SEC_REGULATORY"
    OKX_INCIDENT = "OKX_INCIDENT"
    OKX_MAINTENANCE = "OKX_MAINTENANCE"
    OKX_TRADING_STATUS = "OKX_TRADING_STATUS"


class OfficialEvent(BaseModel):
    """Normalized, bounded event data. Raw external text is never forwarded to an LLM."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: OfficialEventType
    source: str
    source_url: str
    scheduled_at: datetime
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    summary: str = Field(min_length=1, max_length=500)
    severity: str = "high"
    status: str = "scheduled"
    source_hash: str

    @model_validator(mode="after")
    def trusted_source(self) -> Self:
        expected = OFFICIAL_SOURCES.get(self.source)
        if not expected or not self.source_url.startswith(expected.rstrip("/")):
            raise ValueError("event source is not on the official allowlist")
        return self


def normalize_official_event(
    *,
    event_type: OfficialEventType,
    source: str,
    scheduled_at: datetime,
    summary: str,
    source_payload: str,
    event_id: str | None = None,
    severity: str = "high",
    status: str = "scheduled",
) -> OfficialEvent:
    if source not in OFFICIAL_SOURCES:
        raise ValueError("unsupported official source")
    normalized_summary = " ".join(summary.split())[:500]
    digest = sha256(source_payload.encode()).hexdigest()
    identifier = event_id or f"{source.lower()}_{event_type.lower()}_{digest[:20]}"
    return OfficialEvent(
        event_id=identifier,
        event_type=event_type,
        source=source,
        source_url=OFFICIAL_SOURCES[source],
        scheduled_at=scheduled_at,
        summary=normalized_summary,
        severity=severity,
        status=status,
        source_hash=digest,
    )


class EventRiskWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: OfficialEventType
    starts_at: datetime
    ends_at: datetime | None
    blocks_new_risk: bool
    stop_new_risk_until_recovery: bool = False
    source: str

    def active(self, at: datetime | None = None) -> bool:
        current = at or datetime.now(UTC)
        return self.starts_at <= current and (self.ends_at is None or current <= self.ends_at)


WINDOWS: dict[OfficialEventType, tuple[timedelta, timedelta | None]] = {
    OfficialEventType.CPI: (timedelta(minutes=15), timedelta(minutes=5)),
    OfficialEventType.NFP: (timedelta(minutes=15), timedelta(minutes=5)),
    OfficialEventType.FOMC: (timedelta(minutes=30), timedelta(minutes=15)),
    OfficialEventType.SEC_REGULATORY: (timedelta(0), timedelta(minutes=15)),
    OfficialEventType.OKX_INCIDENT: (timedelta(0), None),
    OfficialEventType.OKX_MAINTENANCE: (timedelta(0), None),
    OfficialEventType.OKX_TRADING_STATUS: (timedelta(0), None),
}


def risk_window(event: OfficialEvent) -> EventRiskWindow:
    before, after = WINDOWS[event.event_type]
    outage = event.event_type in {
        OfficialEventType.OKX_INCIDENT,
        OfficialEventType.OKX_MAINTENANCE,
        OfficialEventType.OKX_TRADING_STATUS,
    }
    return EventRiskWindow(
        event_id=event.event_id,
        event_type=event.event_type,
        starts_at=event.scheduled_at - before,
        ends_at=None if outage else event.scheduled_at + (after or timedelta(0)),
        blocks_new_risk=True,
        stop_new_risk_until_recovery=outage,
        source=event.source,
    )


def active_risk_windows(
    events: list[OfficialEvent], at: datetime | None = None
) -> list[dict[str, Any]]:
    current = at or datetime.now(UTC)
    return [
        window.model_dump(mode="json")
        for window in (risk_window(event) for event in events)
        if window.active(current)
    ]


ETF_FLOW_STATUS = {
    "health": "unavailable",
    "reason": "no reliable official free real-time source",
}


def _unfold_icalendar(payload: str) -> list[str]:
    lines: list[str] = []
    for raw in payload.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw.strip())
    return lines


def _parse_icalendar_datetime(name: str, value: str) -> datetime:
    timezone_name = "America/New_York"
    if "TZID=" in name:
        timezone_name = name.split("TZID=", 1)[1].split(";", 1)[0]
    timezone_name = {
        "US-Eastern": "America/New_York",
        "Eastern Standard Time": "America/New_York",
    }.get(timezone_name, timezone_name)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if "T" not in value:
        return datetime.strptime(value, "%Y%m%d").replace(
            tzinfo=ZoneInfo(timezone_name)
        ).astimezone(UTC)
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def parse_bls_calendar(
    payload: str,
    *,
    now: datetime | None = None,
    horizon: timedelta = timedelta(days=400),
) -> list[OfficialEvent]:
    """Parse only CPI and Employment Situation entries from BLS' official ICS."""

    current = now or datetime.now(UTC)
    lines = _unfold_icalendar(payload)
    blocks: list[list[str]] = []
    active: list[str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            active = []
        elif line == "END:VEVENT" and active is not None:
            blocks.append(active)
            active = None
        elif active is not None:
            active.append(line)
    events: list[OfficialEvent] = []
    for block in blocks:
        fields: dict[str, tuple[str, str]] = {}
        for line in block:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields[name.split(";", 1)[0]] = (name, value)
        summary = fields.get("SUMMARY", ("", ""))[1]
        lowered = summary.casefold()
        if "consumer price index" in lowered:
            event_type = OfficialEventType.CPI
            normalized_summary = "BLS Consumer Price Index release"
        elif "employment situation" in lowered:
            event_type = OfficialEventType.NFP
            normalized_summary = "BLS Employment Situation release"
        else:
            continue
        date_field = fields.get("DTSTART")
        if not date_field:
            continue
        scheduled_at = _parse_icalendar_datetime(*date_field)
        if scheduled_at < current - timedelta(hours=1) or scheduled_at > current + horizon:
            continue
        source_payload = "\n".join(block)
        events.append(
            normalize_official_event(
                event_type=event_type,
                source="BLS",
                scheduled_at=scheduled_at,
                summary=normalized_summary,
                source_payload=source_payload,
                event_id=fields.get("UID", ("", ""))[1] or None,
            )
        )
    return sorted(events, key=lambda item: item.scheduled_at)


def parse_okx_system_status(
    payload: dict[str, Any], *, now: datetime | None = None
) -> list[OfficialEvent]:
    """Normalize active/future OKX status records without forwarding provider prose."""

    current = now or datetime.now(UTC)
    events: list[OfficialEvent] = []
    for item in payload.get("data", []):
        state = str(item.get("state") or item.get("status") or "scheduled").casefold()
        if state in {"completed", "resolved", "ended", "2"}:
            continue
        raw_time = item.get("begin") or item.get("scheduledAt") or item.get("startTime")
        try:
            scheduled_at = datetime.fromtimestamp(int(raw_time) / 1000, UTC)
        except (TypeError, ValueError, OSError):
            scheduled_at = current
        item_type = str(item.get("type") or item.get("eventType") or "").casefold()
        event_type = (
            OfficialEventType.OKX_INCIDENT
            if any(token in item_type for token in ("incident", "outage", "failure"))
            else OfficialEventType.OKX_MAINTENANCE
        )
        identifier = str(item.get("id") or item.get("eventId") or "") or None
        canonical = repr(sorted((str(key), str(value)) for key, value in item.items()))
        events.append(
            normalize_official_event(
                event_type=event_type,
                source="OKX",
                scheduled_at=scheduled_at,
                summary=f"OKX system event; status={state[:40]}",
                source_payload=canonical,
                event_id=identifier,
                severity="critical",
                status=state[:40],
            )
        )
    return sorted(events, key=lambda item: item.scheduled_at)


def _plain_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def parse_fomc_calendar(
    payload: str,
    *,
    now: datetime | None = None,
    horizon: timedelta = timedelta(days=400),
) -> list[OfficialEvent]:
    """Parse scheduled FOMC decision times from the Federal Reserve calendar."""

    current = now or datetime.now(UTC)
    year_markers = list(
        re.finditer(r">\s*(20\d{2})\s+FOMC Meetings\s*<", payload, re.IGNORECASE)
    )
    events: list[OfficialEvent] = []
    eastern = ZoneInfo("America/New_York")
    for index, marker in enumerate(year_markers):
        year = int(marker.group(1))
        end = year_markers[index + 1].start() if index + 1 < len(year_markers) else len(payload)
        section = payload[marker.end() : end]
        rows = re.findall(
            r'<div\s+class="row\s+fomc-meeting"[^>]*>(.*?)(?=<div\s+class="row\s+fomc-meeting"|$)',
            section,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for row in rows:
            month_match = re.search(
                r'fomc-meeting__month[^>]*>(.*?)</',
                row,
                flags=re.IGNORECASE | re.DOTALL,
            )
            date_match = re.search(
                r'fomc-meeting__date[^>]*>(.*?)</',
                row,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not month_match or not date_match:
                continue
            month_text = _plain_html(month_match.group(1)).split("/")[0].strip()
            day_values = re.findall(r"\d{1,2}", _plain_html(date_match.group(1)))
            if not day_values:
                continue
            try:
                month = datetime.strptime(month_text[:3], "%b").month
                decision_day = int(day_values[-1])
                scheduled_at = datetime(year, month, decision_day, 14, tzinfo=eastern).astimezone(
                    UTC
                )
            except ValueError:
                continue
            if scheduled_at < current - timedelta(hours=1) or scheduled_at > current + horizon:
                continue
            canonical = f"{year}:{month}:{decision_day}:{_plain_html(row)}"
            events.append(
                normalize_official_event(
                    event_type=OfficialEventType.FOMC,
                    source="FEDERAL_RESERVE",
                    scheduled_at=scheduled_at,
                    summary="Federal Reserve FOMC policy decision",
                    source_payload=canonical,
                    event_id=f"fomc_{year}_{month:02d}_{decision_day:02d}",
                )
            )
    return sorted(events, key=lambda item: item.scheduled_at)


SEC_DIGITAL_ASSET_TERMS = (
    "bitcoin",
    "crypto",
    "digital asset",
    "ether",
    "token",
    "blockchain",
    "virtual currency",
)


def parse_sec_press_releases(
    payload: str,
    *,
    now: datetime | None = None,
    lookback: timedelta = timedelta(days=2),
    horizon: timedelta = timedelta(days=7),
) -> list[OfficialEvent]:
    """Normalize only digital-asset-related SEC RSS releases; prose never escapes."""

    current = now or datetime.now(UTC)
    root = ElementTree.fromstring(payload)
    events: list[OfficialEvent] = []
    for item in root.findall(".//item"):
        title = " ".join((item.findtext("title") or "").split())
        description = " ".join((item.findtext("description") or "").split())
        searchable = f"{title} {description}".casefold()
        if not any(term in searchable for term in SEC_DIGITAL_ASSET_TERMS):
            continue
        published = item.findtext("pubDate")
        if not published:
            continue
        try:
            scheduled_at = parsedate_to_datetime(published).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            continue
        if scheduled_at < current - lookback or scheduled_at > current + horizon:
            continue
        guid = item.findtext("guid") or item.findtext("link") or None
        canonical = "|".join((title, description, published, guid or ""))
        events.append(
            normalize_official_event(
                event_type=OfficialEventType.SEC_REGULATORY,
                source="SEC",
                scheduled_at=scheduled_at,
                summary="SEC digital-asset regulatory press release",
                source_payload=canonical,
                event_id=(f"sec_{sha256(guid.encode()).hexdigest()[:20]}" if guid else None),
                status="published",
            )
        )
    return sorted(events, key=lambda event: event.scheduled_at)


class OfficialEventClient:
    """Bounded official-source collector. Returned objects never contain raw web text."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def fetch_bls(self, *, now: datetime | None = None) -> list[OfficialEvent]:
        return parse_bls_calendar(await self._get_text(OFFICIAL_SOURCES["BLS"]), now=now)

    async def fetch_okx(self, *, now: datetime | None = None) -> list[OfficialEvent]:
        payload = await self._get_json("https://www.okx.com/api/v5/system/status")
        return parse_okx_system_status(payload, now=now)

    async def fetch_fomc(self, *, now: datetime | None = None) -> list[OfficialEvent]:
        return parse_fomc_calendar(
            await self._get_text(OFFICIAL_SOURCES["FEDERAL_RESERVE"]),
            now=now,
        )

    async def fetch_sec(self, *, now: datetime | None = None) -> list[OfficialEvent]:
        return parse_sec_press_releases(await self._get_text(SEC_RSS_URL), now=now)

    async def fetch_all(
        self, *, now: datetime | None = None
    ) -> tuple[list[OfficialEvent], dict[str, str]]:
        events: list[OfficialEvent] = []
        health = {
            "BLS": "unavailable",
            "FEDERAL_RESERVE": "unavailable",
            "SEC": "unavailable",
            "OKX": "unavailable",
            "ETF_FLOW": "unavailable",
        }
        fetchers = (
            ("BLS", self.fetch_bls),
            ("FEDERAL_RESERVE", self.fetch_fomc),
            ("SEC", self.fetch_sec),
            ("OKX", self.fetch_okx),
        )
        results = await asyncio.gather(
            *(fetcher(now=now) for _, fetcher in fetchers),
            return_exceptions=True,
        )
        for (source, _), result in zip(fetchers, results, strict=True):
            if isinstance(result, Exception):
                health[source] = "degraded"
            else:
                events.extend(result)
                health[source] = "healthy"
        unique = {event.event_id: event for event in events}
        return sorted(unique.values(), key=lambda item: item.scheduled_at), health

    async def _get_text(self, url: str) -> str:
        if self._client is not None:
            response = await self._client.get(url)
            response.raise_for_status()
            return response.text
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "COSMOS/2.0 official-event-ingestion",
                    "Accept": (
                        "text/calendar, application/rss+xml, application/xml, "
                        "text/html;q=0.9, */*;q=0.5"
                    ),
                },
            )
            response.raise_for_status()
            return response.text

    async def _get_json(self, url: str) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.get(url)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "COSMOS/2.0 official-event-ingestion",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()
