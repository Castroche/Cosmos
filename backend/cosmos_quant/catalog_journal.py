from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .research_universe import MarketCatalogEntry, catalog_archive_sha256


class MarketCatalogChangeEvent(BaseModel):
    id: str
    exchange: str
    market_type: Literal["spot", "swap", "future"]
    market_id: str
    change_type: Literal["added", "removed", "modified"]
    observed_at: datetime
    previous_snapshot_id: str | None = None
    snapshot_id: str
    before: MarketCatalogEntry | None = None
    after: MarketCatalogEntry | None = None
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketCatalogJournalSnapshot(BaseModel):
    version: int = 1
    id: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    exchange: str
    market_type: Literal["spot", "swap", "future"]
    observed_at: datetime
    source_url: str = Field(pattern=r"^https://")
    entries: list[MarketCatalogEntry]
    record_count: int = Field(ge=0)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_snapshot_id: str | None = None
    previous_chain_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def chain_parent_is_complete(self):
        if (self.previous_snapshot_id is None) != (self.previous_chain_sha256 is None):
            raise ValueError("catalog journal parent id and chain hash must appear together")
        return self


class MarketCatalogJournalCoverage(BaseModel):
    exchange: str
    market_type: Literal["spot", "swap", "future"]
    initial_full_snapshot_at: datetime | None = None
    event_coverage_end: datetime | None = None
    observation_count: int = Field(ge=0)
    change_event_count: int = Field(ge=0)
    maximum_observation_gap_seconds: Decimal = Field(ge=0)
    maximum_allowed_gap_seconds: Decimal = Field(gt=0)
    chain_valid: bool
    observation_schedule_gap_free: bool
    source_kind: Literal["bounded_official_catalog_polling"] = (
        "bounded_official_catalog_polling"
    )
    historical_survivorship_complete: bool = False
    reasons: list[str] = Field(default_factory=list)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _snapshot_chain_sha256(
    *,
    exchange: str,
    market_type: str,
    observed_at: datetime,
    source_url: str,
    payload_sha256: str,
    previous_snapshot_id: str | None,
    previous_chain_sha256: str | None,
) -> str:
    return sha256(
        _canonical_json(
            {
                "exchange": exchange,
                "market_type": market_type,
                "observed_at": observed_at.isoformat(),
                "source_url": source_url,
                "payload_sha256": payload_sha256,
                "previous_snapshot_id": previous_snapshot_id,
                "previous_chain_sha256": previous_chain_sha256,
            }
        )
    ).hexdigest()


def _event_payload(
    *,
    exchange: str,
    market_type: str,
    market_id: str,
    change_type: str,
    observed_at: datetime,
    previous_snapshot_id: str | None,
    snapshot_id: str,
    before: MarketCatalogEntry | None,
    after: MarketCatalogEntry | None,
) -> dict:
    return {
        "exchange": exchange,
        "market_type": market_type,
        "market_id": market_id,
        "change_type": change_type,
        "observed_at": observed_at.isoformat(),
        "previous_snapshot_id": previous_snapshot_id,
        "snapshot_id": snapshot_id,
        "before": before.model_dump(mode="json") if before else None,
        "after": after.model_dump(mode="json") if after else None,
    }


def build_catalog_journal_observation(
    entries: list[MarketCatalogEntry],
    *,
    exchange: str,
    market_type: Literal["spot", "swap", "future"],
    source_url: str,
    observed_at: datetime | None = None,
    previous: MarketCatalogJournalSnapshot | None = None,
) -> tuple[MarketCatalogJournalSnapshot, list[MarketCatalogChangeEvent]]:
    observation_time = observed_at or datetime.now(UTC)
    if observation_time.tzinfo is None:
        raise ValueError("catalog journal observation time must be timezone-aware")
    if previous and (
        previous.exchange != exchange or previous.market_type != market_type
    ):
        raise ValueError("catalog journal parent scope mismatch")
    if previous and observation_time <= previous.observed_at:
        raise ValueError("catalog journal observations must be strictly chronological")
    scoped = sorted(entries, key=lambda item: (item.market_id, item.symbol))
    if any(item.exchange != exchange or item.market_type != market_type for item in scoped):
        raise ValueError("catalog journal entries must exactly match their scope")
    if len({item.market_id for item in scoped}) != len(scoped):
        raise ValueError("catalog journal contains duplicate market identifiers")
    payload_hash = catalog_archive_sha256(scoped)
    chain_hash = _snapshot_chain_sha256(
        exchange=exchange,
        market_type=market_type,
        observed_at=observation_time,
        source_url=source_url,
        payload_sha256=payload_hash,
        previous_snapshot_id=previous.id if previous else None,
        previous_chain_sha256=previous.chain_sha256 if previous else None,
    )
    snapshot = MarketCatalogJournalSnapshot(
        id=f"catalog_{chain_hash[:24]}",
        fingerprint=chain_hash,
        exchange=exchange,
        market_type=market_type,
        observed_at=observation_time,
        source_url=source_url,
        entries=scoped,
        record_count=len(scoped),
        payload_sha256=payload_hash,
        previous_snapshot_id=previous.id if previous else None,
        previous_chain_sha256=previous.chain_sha256 if previous else None,
        chain_sha256=chain_hash,
    )
    before_by_id = {item.market_id: item for item in previous.entries} if previous else {}
    after_by_id = {item.market_id: item for item in scoped}
    events: list[MarketCatalogChangeEvent] = []
    for market_id in sorted(set(before_by_id) | set(after_by_id)):
        before = before_by_id.get(market_id)
        after = after_by_id.get(market_id)
        if before is None:
            change_type = "added"
        elif after is None:
            change_type = "removed"
        elif before.model_dump(mode="json") != after.model_dump(mode="json"):
            change_type = "modified"
        else:
            continue
        payload = _event_payload(
            exchange=exchange,
            market_type=market_type,
            market_id=market_id,
            change_type=change_type,
            observed_at=observation_time,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            snapshot_id=snapshot.id,
            before=before,
            after=after,
        )
        fingerprint = sha256(_canonical_json(payload)).hexdigest()
        events.append(
            MarketCatalogChangeEvent(
                id=f"catalogevent_{fingerprint[:24]}",
                fingerprint=fingerprint,
                before=before,
                after=after,
                **{
                    key: value
                    for key, value in payload.items()
                    if key not in {"before", "after"}
                },
            )
        )
    validate_catalog_journal_observation(snapshot, events, previous=previous)
    return snapshot, events


def validate_catalog_journal_observation(
    snapshot: MarketCatalogJournalSnapshot,
    events: list[MarketCatalogChangeEvent],
    *,
    previous: MarketCatalogJournalSnapshot | None,
) -> None:
    if snapshot.version != 1:
        raise ValueError("unsupported catalog journal version")
    if snapshot.record_count != len(snapshot.entries):
        raise ValueError("catalog journal record count mismatch")
    if snapshot.payload_sha256 != catalog_archive_sha256(snapshot.entries):
        raise ValueError("catalog journal payload hash mismatch")
    expected_chain = _snapshot_chain_sha256(
        exchange=snapshot.exchange,
        market_type=snapshot.market_type,
        observed_at=snapshot.observed_at,
        source_url=snapshot.source_url,
        payload_sha256=snapshot.payload_sha256,
        previous_snapshot_id=previous.id if previous else None,
        previous_chain_sha256=previous.chain_sha256 if previous else None,
    )
    if (
        snapshot.previous_snapshot_id != (previous.id if previous else None)
        or snapshot.previous_chain_sha256 != (previous.chain_sha256 if previous else None)
        or snapshot.chain_sha256 != expected_chain
        or snapshot.fingerprint != expected_chain
        or snapshot.id != f"catalog_{expected_chain[:24]}"
    ):
        raise ValueError("catalog journal chain binding mismatch")
    if any(
        item.exchange != snapshot.exchange or item.market_type != snapshot.market_type
        for item in snapshot.entries
    ):
        raise ValueError("catalog journal entries no longer match their scope")
    if len({item.market_id for item in snapshot.entries}) != len(snapshot.entries):
        raise ValueError("catalog journal contains duplicate market identifiers")
    before_by_id = {item.market_id: item for item in previous.entries} if previous else {}
    after_by_id = {item.market_id: item for item in snapshot.entries}
    expected_fingerprints: set[str] = set()
    for market_id in sorted(set(before_by_id) | set(after_by_id)):
        before = before_by_id.get(market_id)
        after = after_by_id.get(market_id)
        change_type = (
            "added" if before is None else "removed" if after is None else "modified"
        )
        if before is not None and after is not None and (
            before.model_dump(mode="json") == after.model_dump(mode="json")
        ):
            continue
        payload = _event_payload(
            exchange=snapshot.exchange,
            market_type=snapshot.market_type,
            market_id=market_id,
            change_type=change_type,
            observed_at=snapshot.observed_at,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            snapshot_id=snapshot.id,
            before=before,
            after=after,
        )
        expected_fingerprints.add(sha256(_canonical_json(payload)).hexdigest())
    actual_fingerprints = {event.fingerprint for event in events}
    if len(actual_fingerprints) != len(events) or actual_fingerprints != expected_fingerprints:
        raise ValueError("catalog journal change events do not match the snapshot diff")
    for event in events:
        payload = _event_payload(
            exchange=event.exchange,
            market_type=event.market_type,
            market_id=event.market_id,
            change_type=event.change_type,
            observed_at=event.observed_at,
            previous_snapshot_id=event.previous_snapshot_id,
            snapshot_id=event.snapshot_id,
            before=event.before,
            after=event.after,
        )
        fingerprint = sha256(_canonical_json(payload)).hexdigest()
        if event.fingerprint != fingerprint or event.id != f"catalogevent_{fingerprint[:24]}":
            raise ValueError("catalog journal event fingerprint mismatch")


def evaluate_catalog_journal_coverage(
    snapshots: list[MarketCatalogJournalSnapshot],
    events: list[MarketCatalogChangeEvent],
    *,
    exchange: str,
    market_type: Literal["spot", "swap", "future"],
    maximum_allowed_gap_seconds: Decimal = Decimal("4500"),
    now: datetime | None = None,
) -> MarketCatalogJournalCoverage:
    scoped_snapshots = sorted(
        (
            item
            for item in snapshots
            if item.exchange == exchange and item.market_type == market_type
        ),
        key=lambda item: item.observed_at,
    )
    scoped_events = [
        item
        for item in events
        if item.exchange == exchange and item.market_type == market_type
    ]
    events_by_snapshot: dict[str, list[MarketCatalogChangeEvent]] = {}
    for event in scoped_events:
        events_by_snapshot.setdefault(event.snapshot_id, []).append(event)
    reasons: list[str] = []
    chain_valid = True
    previous: MarketCatalogJournalSnapshot | None = None
    for snapshot in scoped_snapshots:
        try:
            validate_catalog_journal_observation(
                snapshot,
                events_by_snapshot.get(snapshot.id, []),
                previous=previous,
            )
        except ValueError as exc:
            chain_valid = False
            reasons.append(f"{snapshot.id}: {exc}")
        previous = snapshot
    if not scoped_snapshots:
        chain_valid = False
        reasons.append("no initial full catalog snapshot")
    observation_end = now or datetime.now(UTC)
    gaps = [
        Decimal(str((current.observed_at - prior.observed_at).total_seconds()))
        for prior, current in zip(
            scoped_snapshots,
            scoped_snapshots[1:],
            strict=False,
        )
    ]
    if scoped_snapshots:
        gaps.append(
            Decimal(str((observation_end - scoped_snapshots[-1].observed_at).total_seconds()))
        )
    maximum_gap = max(gaps, default=Decimal("0"))
    schedule_gap_free = bool(
        chain_valid
        and len(scoped_snapshots) >= 2
        and maximum_gap <= maximum_allowed_gap_seconds
    )
    if len(scoped_snapshots) < 2:
        reasons.append("at least two scheduled observations are required")
    if maximum_gap > maximum_allowed_gap_seconds:
        reasons.append(
            f"maximum observation gap {maximum_gap}s exceeds "
            f"{maximum_allowed_gap_seconds}s"
        )
    # Polling creates valuable prospective survivorship evidence but cannot be
    # mislabeled as an official gap-free exchange event archive.
    reasons.append(
        "bounded polling is not an official gap-free listing/delisting event feed"
    )
    return MarketCatalogJournalCoverage(
        exchange=exchange,
        market_type=market_type,
        initial_full_snapshot_at=(
            scoped_snapshots[0].observed_at if scoped_snapshots else None
        ),
        event_coverage_end=(
            scoped_snapshots[-1].observed_at if scoped_snapshots else None
        ),
        observation_count=len(scoped_snapshots),
        change_event_count=len(scoped_events),
        maximum_observation_gap_seconds=max(maximum_gap, Decimal("0")),
        maximum_allowed_gap_seconds=maximum_allowed_gap_seconds,
        chain_valid=chain_valid,
        observation_schedule_gap_free=schedule_gap_free,
        historical_survivorship_complete=False,
        reasons=reasons,
    )
