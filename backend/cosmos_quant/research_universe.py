from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


class MarketCatalogEntry(BaseModel):
    exchange: str
    market_id: str
    symbol: str
    base: str
    quote: str
    settle: str | None = None
    market_type: str
    active: bool | None = None
    contract: bool = False
    linear: bool | None = None
    inverse: bool | None = None
    listed_at: datetime | None = None
    listing_time_source: str | None = None
    expiry: datetime | None = None
    status: str | None = None
    quote_volume_24h: Decimal | None = None
    asset_class: Literal["crypto", "non_crypto", "unknown"] = "unknown"
    asset_class_source: str | None = None
    underlying_type: str | None = None
    underlying_subtypes: list[str] = Field(default_factory=list)
    catalog_source: str = "ccxt_load_markets"


class PointInTimeUniversePolicy(BaseModel):
    quote_currency: str = "USDT"
    market_type: Literal["spot", "swap", "future"] = "swap"
    timeframes: list[str] = Field(min_length=1)
    allowed_symbols: list[str] = Field(default_factory=list)
    excluded_bases: list[str] = Field(
        default_factory=lambda: [
            "USD",
            "USDT",
            "USDC",
            "FDUSD",
            "DAI",
            "EUR",
            "GBP",
            "JPY",
            "AUD",
            "CAD",
            "CHF",
            "XAU",
            "XAG",
        ]
    )
    selection_scope: Literal["per_exchange", "common_across_exchanges"] = "common_across_exchanges"
    require_active: bool = True
    require_linear_contract: bool = True
    exclude_verified_non_crypto: bool = True
    minimum_listing_age_days: int = Field(default=90, ge=0, le=3650)
    minimum_quote_volume_24h: Decimal = Field(default=Decimal("0"), ge=0)
    required_bars_by_timeframe: dict[str, int] = Field(default_factory=dict)
    minimum_history_coverage_ratio: Decimal = Field(
        default=Decimal("0.90"),
        gt=0,
        le=1,
    )
    minimum_symbols_per_exchange: int = Field(default=3, ge=1, le=50)
    maximum_symbols_per_exchange: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def normalize_history_requirements(self):
        if not self.required_bars_by_timeframe:
            self.required_bars_by_timeframe = {timeframe: 500 for timeframe in self.timeframes}
        if set(self.required_bars_by_timeframe) != set(self.timeframes):
            raise ValueError("required_bars_by_timeframe must exactly match policy timeframes")
        if any(bars < 1 or bars > 50000 for bars in self.required_bars_by_timeframe.values()):
            raise ValueError("required history bars must be between 1 and 50000")
        return self


class UniverseInstrumentEvidence(BaseModel):
    entry: MarketCatalogEntry
    eligible: bool
    selected: bool = False
    exclusion_reasons: list[str] = Field(default_factory=list)
    rank: int | None = None


class HistoricalCatalogArchiveEvidence(BaseModel):
    exchange: str
    market_type: Literal["spot", "swap", "future"]
    archive_as_of: datetime
    source_kind: Literal[
        "official_point_in_time_archive",
        "event_sourced_catalog_journal",
        "trusted_web_archive_of_official_endpoint",
    ]
    source_url: str = Field(pattern=r"^https://")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=1)
    initial_full_snapshot_at: datetime | None = None
    event_coverage_end: datetime | None = None
    event_log_gap_free: bool = False
    archive_provider: Literal["arquivo_pt", "internet_archive"] | None = None
    archive_capture_at: datetime | None = None
    original_source_url: str | None = Field(default=None, pattern=r"^https://")
    archive_index_url: str | None = Field(default=None, pattern=r"^https://")
    archive_index_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archive_digest_sha1_base32: str | None = Field(
        default=None,
        pattern=r"^[A-Z2-7]{32}$",
    )
    raw_payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_payload_path: str | None = None
    archive_index_path: str | None = None
    original_server_time: datetime | None = None
    capture_lag_seconds: int | None = Field(default=None, ge=0, le=3600)


class ResearchUniverseSnapshot(BaseModel):
    version: int = 2
    id: str
    fingerprint: str
    as_of: datetime
    exchange_ids: list[str]
    policy: PointInTimeUniversePolicy
    instruments: list[UniverseInstrumentEvidence]
    selected_markets: list[str]
    selected_market_count: int
    catalog_complete: bool
    point_in_time_selection_passed: bool
    historical_survivorship_complete: bool = False
    survivorship_scope: Literal[
        "prospective_point_in_time",
        "historical_archive",
        "uncontrolled",
    ] = "prospective_point_in_time"
    archive_evidence: list[HistoricalCatalogArchiveEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def catalog_archive_sha256(entries: list[MarketCatalogEntry]) -> str:
    payload = [
        item.model_dump(mode="json")
        for item in sorted(entries, key=lambda value: (value.market_id, value.symbol))
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _snapshot_payload(
    *,
    version: int,
    as_of: datetime,
    exchange_ids: list[str],
    policy: PointInTimeUniversePolicy,
    instruments: list[UniverseInstrumentEvidence],
    catalog_complete: bool,
    historical_survivorship_complete: bool,
    survivorship_scope: str,
    archive_evidence: list[HistoricalCatalogArchiveEvidence] | None = None,
) -> dict:
    policy_payload = policy.model_dump(mode="json")
    instrument_payloads = [
        item.model_dump(mode="json")
        for item in sorted(
            instruments,
            key=lambda value: (
                value.entry.exchange,
                value.entry.market_id,
                value.entry.symbol,
            ),
        )
    ]
    # Version 1 artifacts predate the materialized-history and asset-class
    # evidence. Preserve their original canonical payload so persisted audit
    # records remain verifiable after the schema upgrade.
    if version == 1:
        for key in (
            "exclude_verified_non_crypto",
            "required_bars_by_timeframe",
            "minimum_history_coverage_ratio",
        ):
            policy_payload.pop(key, None)
        for item in instrument_payloads:
            entry = item["entry"]
            for key in (
                "asset_class",
                "asset_class_source",
                "underlying_type",
                "underlying_subtypes",
            ):
                entry.pop(key, None)
    payload = {
        "version": version,
        "as_of": as_of.isoformat(),
        "exchange_ids": sorted(exchange_ids),
        "policy": policy_payload,
        "catalog_complete": catalog_complete,
        "historical_survivorship_complete": historical_survivorship_complete,
        "survivorship_scope": survivorship_scope,
        "instruments": instrument_payloads,
    }
    if version >= 3:
        evidence_payloads = [
            item.model_dump(mode="json")
            for item in sorted(
                archive_evidence or [],
                key=lambda value: (value.exchange, value.market_type),
            )
        ]
        if version == 3:
            new_fields = {
                "archive_provider",
                "archive_capture_at",
                "original_source_url",
                "archive_index_url",
                "archive_index_sha256",
                "archive_digest_sha1_base32",
                "raw_payload_sha256",
                "raw_payload_path",
                "archive_index_path",
                "original_server_time",
                "capture_lag_seconds",
            }
            for item in evidence_payloads:
                for field in new_fields:
                    item.pop(field, None)
        payload["archive_evidence"] = evidence_payloads
    return payload


def _validate_historical_archive_evidence(
    *,
    catalogs: dict[str, list[MarketCatalogEntry]],
    policy: PointInTimeUniversePolicy,
    as_of: datetime,
    archive_evidence: list[HistoricalCatalogArchiveEvidence],
) -> None:
    by_exchange = {item.exchange: item for item in archive_evidence}
    if len(by_exchange) != len(archive_evidence) or set(by_exchange) != set(catalogs):
        raise ValueError("historical archive evidence must cover every exchange exactly once")
    latest_archive_as_of = max(item.archive_as_of for item in archive_evidence)
    if latest_archive_as_of != as_of:
        raise ValueError("universe snapshot time must equal the latest archive observation")
    for exchange, entries in catalogs.items():
        evidence = by_exchange[exchange]
        if any(
            item.exchange != exchange or item.market_type != policy.market_type for item in entries
        ):
            raise ValueError(
                "historical archive entries must match their exchange and market type scope"
            )
        if len({item.market_id for item in entries}) != len(entries):
            raise ValueError("historical archive contains duplicate market identifiers")
        if evidence.archive_as_of > as_of:
            raise ValueError("historical archive observation cannot follow the snapshot time")
        if evidence.archive_as_of != as_of:
            if not policy.allowed_symbols:
                raise ValueError(
                    "mixed-time historical archives require an explicit fixed symbol scope"
                )
            observed_symbols = {item.symbol for item in entries}
            missing_symbols = sorted(set(policy.allowed_symbols) - observed_symbols)
            if missing_symbols:
                raise ValueError(
                    "mixed-time historical archive misses fixed-scope symbols: "
                    + ", ".join(missing_symbols)
                )
        if evidence.market_type != policy.market_type:
            raise ValueError("historical archive market type does not match universe policy")
        if evidence.record_count != len(entries):
            raise ValueError("historical archive record count does not match its catalog")
        if evidence.payload_sha256 != catalog_archive_sha256(entries):
            raise ValueError("historical archive payload hash does not match its catalog")
        if evidence.source_kind == "event_sourced_catalog_journal" and (
            evidence.initial_full_snapshot_at is None
            or evidence.event_coverage_end is None
            or evidence.initial_full_snapshot_at > as_of
            or evidence.event_coverage_end < as_of
            or not evidence.event_log_gap_free
        ):
            raise ValueError(
                "event-sourced archive requires an initial full snapshot and "
                "gap-free events through as_of"
            )
        if evidence.source_kind == "trusted_web_archive_of_official_endpoint":
            required = (
                evidence.archive_provider,
                evidence.archive_capture_at,
                evidence.original_source_url,
                evidence.archive_index_url,
                evidence.archive_index_sha256,
                evidence.archive_digest_sha1_base32,
                evidence.raw_payload_sha256,
                evidence.raw_payload_path,
                evidence.archive_index_path,
                evidence.original_server_time,
                evidence.capture_lag_seconds,
            )
            if any(value is None for value in required):
                raise ValueError("trusted web archive evidence is incomplete")
            if evidence.original_server_time != evidence.archive_as_of:
                raise ValueError(
                    "trusted web archive server time must equal its archive observation"
                )
            if evidence.archive_capture_at < evidence.original_server_time:
                raise ValueError("trusted web archive capture predates original server time")
            expected_lag = int(
                (evidence.archive_capture_at - evidence.original_server_time).total_seconds()
            )
            if evidence.capture_lag_seconds != expected_lag:
                raise ValueError("trusted web archive capture lag is inconsistent")
            original = urlparse(evidence.original_source_url)
            replay = urlparse(evidence.source_url)
            index = urlparse(evidence.archive_index_url)
            arquivo_scope = bool(
                evidence.archive_provider == "arquivo_pt"
                and exchange == "binance"
                and original.scheme == "https"
                and original.hostname == "fapi.binance.com"
                and original.path == "/fapi/v1/exchangeInfo"
                and not original.query
                and replay.scheme == "https"
                and replay.hostname == "arquivo.pt"
                and replay.path.startswith("/noFrame/replay/")
                and index.scheme == "https"
                and index.hostname == "arquivo.pt"
                and index.path == "/wayback/cdx"
            )
            internet_archive_binance_scope = bool(
                evidence.archive_provider == "internet_archive"
                and exchange == "binance"
                and original.scheme == "https"
                and original.hostname == "fapi.binance.com"
                and original.path == "/fapi/v1/exchangeInfo"
                and not original.query
                and replay.scheme == "https"
                and replay.hostname == "web.archive.org"
                and replay.path.startswith("/web/")
                and not replay.query
                and index.scheme == "https"
                and index.hostname == "web.archive.org"
                and index.path == "/cdx/search/cdx"
            )
            internet_archive_okx_scope = bool(
                evidence.archive_provider == "internet_archive"
                and exchange == "okx"
                and original.scheme == "https"
                and original.hostname == "www.okx.com"
                and original.path == "/api/v5/public/instruments"
                and original.query == "instType=SWAP"
                and replay.scheme == "https"
                and replay.hostname == "web.archive.org"
                and replay.path.startswith("/web/")
                and replay.query == "instType=SWAP"
                and index.scheme == "https"
                and index.hostname == "web.archive.org"
                and index.path == "/cdx/search/cdx"
            )
            if not (
                arquivo_scope
                or internet_archive_binance_scope
                or internet_archive_okx_scope
            ):
                raise ValueError("trusted web archive source scope is invalid")
            for path in (evidence.raw_payload_path, evidence.archive_index_path):
                candidate = str(path).replace("\\", "/")
                if candidate.startswith("/") or ".." in candidate.split("/"):
                    raise ValueError("trusted web archive local evidence path is unsafe")


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def build_point_in_time_universe_snapshot(
    catalogs: dict[str, list[MarketCatalogEntry]],
    *,
    policy: PointInTimeUniversePolicy,
    as_of: datetime | None = None,
    catalog_errors: dict[str, str] | None = None,
    archive_evidence: list[HistoricalCatalogArchiveEvidence] | None = None,
) -> ResearchUniverseSnapshot:
    """Freeze a deterministic market selection using only snapshot-time evidence.

    A current exchange catalog cannot reconstruct instruments that disappeared
    before the snapshot. The resulting artifact therefore controls prospective
    selection and records historical survivorship as unproven.
    """

    snapshot_time = as_of or datetime.now(UTC)
    if snapshot_time.tzinfo is None:
        raise ValueError("universe snapshot time must be timezone-aware")
    errors = catalog_errors or {}
    exchange_ids = sorted(catalogs)
    if archive_evidence:
        if errors:
            raise ValueError("historical archive cannot be complete with catalog errors")
        _validate_historical_archive_evidence(
            catalogs=catalogs,
            policy=policy,
            as_of=snapshot_time,
            archive_evidence=archive_evidence,
        )
    evidence: list[UniverseInstrumentEvidence] = []
    per_exchange_selected: dict[str, list[UniverseInstrumentEvidence]] = {}
    minimum_listing_time = snapshot_time - timedelta(days=policy.minimum_listing_age_days)
    allowed_symbols = set(policy.allowed_symbols)
    excluded_bases = {base.upper() for base in policy.excluded_bases}

    for exchange in exchange_ids:
        for entry in catalogs[exchange]:
            reasons: list[str] = []
            if allowed_symbols and entry.symbol not in allowed_symbols:
                reasons.append("outside_allowed_symbol_scope")
            if entry.base.upper() in excluded_bases:
                reasons.append("excluded_non_crypto_or_cash_base")
            if policy.exclude_verified_non_crypto and entry.asset_class == "non_crypto":
                reasons.append("verified_non_crypto_underlying")
            if entry.quote.upper() != policy.quote_currency.upper():
                reasons.append("quote_currency_mismatch")
            if entry.market_type != policy.market_type:
                reasons.append("market_type_mismatch")
            if policy.require_active and entry.active is not True:
                reasons.append(
                    "market_inactive" if entry.active is False else "active_status_unknown"
                )
            if (
                policy.market_type in {"swap", "future"}
                and policy.require_linear_contract
                and entry.linear is not True
            ):
                reasons.append("not_verified_linear_contract")
            if entry.expiry is not None and entry.expiry <= snapshot_time:
                reasons.append("contract_expired")
            if policy.minimum_listing_age_days > 0:
                if entry.listed_at is None:
                    reasons.append("listing_time_unknown")
                elif entry.listed_at > minimum_listing_time:
                    reasons.append("listing_age_below_minimum")
            if policy.minimum_quote_volume_24h > 0:
                if entry.quote_volume_24h is None:
                    reasons.append("quote_volume_unknown")
                elif entry.quote_volume_24h < policy.minimum_quote_volume_24h:
                    reasons.append("quote_volume_below_minimum")
            item = UniverseInstrumentEvidence(
                entry=entry,
                eligible=not reasons,
                exclusion_reasons=reasons,
            )
            evidence.append(item)

    candidates_by_exchange: dict[str, list[UniverseInstrumentEvidence]] = {}
    for exchange in exchange_ids:
        ordered = sorted(
            (item for item in evidence if item.entry.exchange == exchange and item.eligible),
            key=lambda item: (
                item.entry.quote_volume_24h is not None,
                item.entry.quote_volume_24h or Decimal("0"),
                -(item.entry.listed_at or snapshot_time).timestamp(),
                item.entry.symbol,
            ),
            reverse=True,
        )
        unique = []
        unique_symbols: set[str] = set()
        for item in ordered:
            if item.entry.symbol in unique_symbols:
                item.eligible = False
                item.exclusion_reasons.append("duplicate_canonical_symbol")
                continue
            unique_symbols.add(item.entry.symbol)
            unique.append(item)
        candidates_by_exchange[exchange] = unique

    if policy.selection_scope == "common_across_exchanges" and len(exchange_ids) > 1:
        common_symbols = set.intersection(
            *(
                {item.entry.symbol for item in candidates_by_exchange[exchange]}
                for exchange in exchange_ids
            )
        )
        for item in evidence:
            if item.eligible and item.entry.symbol not in common_symbols:
                item.eligible = False
                item.exclusion_reasons.append("insufficient_common_exchange_coverage")
        entries_by_symbol = {
            symbol: [
                next(
                    item for item in candidates_by_exchange[exchange] if item.entry.symbol == symbol
                )
                for exchange in exchange_ids
            ]
            for symbol in common_symbols
        }
        ranked_symbols = sorted(
            common_symbols,
            key=lambda symbol: (
                all(item.entry.quote_volume_24h is not None for item in entries_by_symbol[symbol]),
                min(
                    item.entry.quote_volume_24h or Decimal("0")
                    for item in entries_by_symbol[symbol]
                ),
                sum(
                    (
                        item.entry.quote_volume_24h or Decimal("0")
                        for item in entries_by_symbol[symbol]
                    ),
                    Decimal("0"),
                ),
                symbol,
            ),
            reverse=True,
        )[: policy.maximum_symbols_per_exchange]
        for exchange in exchange_ids:
            selected = []
            for rank, symbol in enumerate(ranked_symbols, start=1):
                item = next(
                    value
                    for value in candidates_by_exchange[exchange]
                    if value.entry.symbol == symbol
                )
                item.selected = True
                item.rank = rank
                selected.append(item)
            per_exchange_selected[exchange] = selected
    else:
        for exchange in exchange_ids:
            selected = candidates_by_exchange[exchange][: policy.maximum_symbols_per_exchange]
            for rank, item in enumerate(selected, start=1):
                item.selected = True
                item.rank = rank
            per_exchange_selected[exchange] = selected

    selected_markets = sorted(
        f"{item.entry.exchange}:{item.entry.symbol}" for item in evidence if item.selected
    )
    catalog_complete = bool(exchange_ids) and not errors
    selection_passed = bool(
        catalog_complete
        and all(
            len(per_exchange_selected.get(exchange, [])) >= policy.minimum_symbols_per_exchange
            for exchange in exchange_ids
        )
    )
    historical_survivorship_complete = bool(archive_evidence and catalog_complete)
    version = (
        4
        if archive_evidence
        and any(
            item.source_kind == "trusted_web_archive_of_official_endpoint"
            for item in archive_evidence
        )
        else 3
        if archive_evidence
        else 2
    )
    survivorship_scope = (
        "historical_archive" if historical_survivorship_complete else "prospective_point_in_time"
    )
    limitations = (
        []
        if historical_survivorship_complete
        else [
            "Current exchange catalogs cannot prove coverage of markets delisted "
            "before the snapshot.",
            "Historical survivorship control requires an archived catalog or datasets "
            "collected after this snapshot.",
        ]
    )
    limitations.extend(
        f"{exchange}: catalog unavailable: {message}"
        for exchange, message in sorted(errors.items())
    )
    payload = _snapshot_payload(
        version=version,
        as_of=snapshot_time,
        exchange_ids=exchange_ids,
        policy=policy,
        instruments=evidence,
        catalog_complete=catalog_complete,
        historical_survivorship_complete=historical_survivorship_complete,
        survivorship_scope=survivorship_scope,
        archive_evidence=archive_evidence,
    )
    fingerprint = _fingerprint(payload)
    return ResearchUniverseSnapshot(
        version=version,
        id=f"universe_{fingerprint}",
        fingerprint=fingerprint,
        as_of=snapshot_time,
        exchange_ids=exchange_ids,
        policy=policy,
        instruments=evidence,
        selected_markets=selected_markets,
        selected_market_count=len(selected_markets),
        catalog_complete=catalog_complete,
        point_in_time_selection_passed=selection_passed,
        historical_survivorship_complete=historical_survivorship_complete,
        survivorship_scope=survivorship_scope,
        archive_evidence=list(archive_evidence or []),
        limitations=limitations,
    )


def validate_universe_snapshot(snapshot: ResearchUniverseSnapshot) -> None:
    if snapshot.version not in {1, 2, 3, 4}:
        raise ValueError("unsupported research universe snapshot version")
    if snapshot.version >= 2:
        required_timeframes = set(snapshot.policy.required_bars_by_timeframe)
        if required_timeframes != set(snapshot.policy.timeframes) or any(
            bars < 1 for bars in snapshot.policy.required_bars_by_timeframe.values()
        ):
            raise ValueError("research universe snapshot history requirements are incomplete")
    selected_markets = sorted(
        f"{item.entry.exchange}:{item.entry.symbol}"
        for item in snapshot.instruments
        if item.selected
    )
    selected_per_exchange = {
        exchange: sum(
            item.selected and item.entry.exchange == exchange for item in snapshot.instruments
        )
        for exchange in snapshot.exchange_ids
    }
    expected_selection_passed = bool(
        snapshot.catalog_complete
        and snapshot.exchange_ids
        and all(
            count >= snapshot.policy.minimum_symbols_per_exchange
            for count in selected_per_exchange.values()
        )
    )
    if (
        snapshot.selected_markets != selected_markets
        or snapshot.selected_market_count != len(selected_markets)
        or snapshot.point_in_time_selection_passed != expected_selection_passed
    ):
        raise ValueError("research universe snapshot derived evidence mismatch")
    if (
        snapshot.historical_survivorship_complete
        and snapshot.survivorship_scope != "historical_archive"
    ):
        raise ValueError("historical survivorship requires an archived catalog scope")
    if snapshot.version >= 3:
        catalogs: dict[str, list[MarketCatalogEntry]] = {
            exchange: [
                item.entry for item in snapshot.instruments if item.entry.exchange == exchange
            ]
            for exchange in snapshot.exchange_ids
        }
        _validate_historical_archive_evidence(
            catalogs=catalogs,
            policy=snapshot.policy,
            as_of=snapshot.as_of,
            archive_evidence=snapshot.archive_evidence,
        )
        if (
            not snapshot.historical_survivorship_complete
            or snapshot.survivorship_scope != "historical_archive"
        ):
            raise ValueError("version 3+ universe must carry complete historical archive evidence")
    elif snapshot.archive_evidence:
        raise ValueError("legacy universe snapshots cannot carry archive evidence")
    payload = _snapshot_payload(
        version=snapshot.version,
        as_of=snapshot.as_of,
        exchange_ids=snapshot.exchange_ids,
        policy=snapshot.policy,
        instruments=snapshot.instruments,
        catalog_complete=snapshot.catalog_complete,
        historical_survivorship_complete=snapshot.historical_survivorship_complete,
        survivorship_scope=snapshot.survivorship_scope,
        archive_evidence=snapshot.archive_evidence,
    )
    fingerprint = _fingerprint(payload)
    if snapshot.fingerprint != fingerprint or snapshot.id != f"universe_{fingerprint}":
        raise ValueError("research universe snapshot fingerprint mismatch")
