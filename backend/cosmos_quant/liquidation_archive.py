from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, computed_field, model_validator

from .archive_data import download_verified_archive
from .domain import LiquidationFlowPoint

BINANCE_VISION_BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BINANCE_COIN_M_LIQUIDATION_ROOT = (
    "https://data.binance.vision/data/futures/cm/daily/liquidationSnapshot"
)
_INSTRUMENTS = {
    "BTC/USDT": "BTCUSD_PERP",
    "ETH/USDT": "ETHUSD_PERP",
    "SOL/USDT": "SOLUSD_PERP",
}
_HEADER = [
    "time",
    "side",
    "order_type",
    "time_in_force",
    "original_quantity",
    "price",
    "average_price",
    "order_status",
    "last_fill_quantity",
    "accumulated_fill_quantity",
]


class CoinMLiquidationFlowBar(BaseModel):
    research_symbol: str
    source_instrument_id: str
    source_market_type: str = "coin_m_inverse_perpetual"
    interval_start: datetime
    available_at: datetime
    long_liquidated_contracts: Decimal = Field(ge=0)
    short_liquidated_contracts: Decimal = Field(ge=0)
    unique_event_count: int = Field(ge=1)

    @model_validator(mode="after")
    def causal_close_time(self):
        if self.available_at != self.interval_start + timedelta(hours=1):
            raise ValueError("liquidation flow is available only at the closed hour boundary")
        return self


class CoinMLiquidationFlowSnapshot(BaseModel):
    research_symbol: str
    source_instrument_id: str
    source_market_type: str = "coin_m_inverse_perpetual"
    evidence_role: str = "cross_contract_underlying_liquidation_context"
    as_of: datetime
    lookback_hours: int = Field(ge=1)
    observed_bar_count: int = Field(ge=0)
    unique_event_count: int = Field(ge=0)
    long_liquidated_contracts: Decimal = Field(ge=0)
    short_liquidated_contracts: Decimal = Field(ge=0)
    short_minus_long_imbalance: Decimal = Field(ge=-1, le=1)


class CoinMLiquidationArchiveDay(BaseModel):
    day: date
    research_symbol: str
    source_instrument_id: str
    archive_url: str
    archive_path: str
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size_bytes: int = Field(gt=0)
    raw_row_count: int = Field(ge=0)
    unique_event_count: int = Field(ge=0)


class CoinMLiquidationArchiveManifest(BaseModel):
    schema_version: int = 1
    id: str
    exchange: str = "binance"
    source_market_type: str = "coin_m_inverse_perpetual"
    evidence_role: str = "cross_contract_underlying_liquidation_context"
    source_listing_url: str
    source_listing_path: str
    source_listing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_symbol: str
    source_instrument_id: str
    coverage_start: datetime
    coverage_end: datetime
    expected_calendar_days: int = Field(ge=90)
    archive_day_count: int = Field(ge=90)
    missing_calendar_days: list[str] = Field(default_factory=list)
    raw_row_count: int = Field(ge=0)
    unique_event_count: int = Field(ge=100)
    duplicate_row_count: int = Field(ge=0)
    flow_bar_count: int = Field(ge=1)
    flow_path: str
    flow_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    days: list[CoinMLiquidationArchiveDay]
    limitations: list[str]
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"id", "fingerprint", "retrieved_at"},
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @model_validator(mode="after")
    def complete_calendar_contract(self):
        if self.archive_day_count != self.expected_calendar_days:
            raise ValueError("archive day count does not cover the requested calendar")
        if self.archive_day_count != len(self.days) or self.missing_calendar_days:
            raise ValueError("liquidation archive calendar contains a gap")
        if self.coverage_end <= self.coverage_start:
            raise ValueError("liquidation archive coverage range is invalid")
        if self.coverage_end - self.coverage_start != timedelta(
            days=self.expected_calendar_days
        ):
            raise ValueError("liquidation archive range and calendar count disagree")
        return self


def _validated_instrument(research_symbol: str) -> str:
    try:
        return _INSTRUMENTS[research_symbol]
    except KeyError as exc:
        raise ValueError("unsupported COIN-M liquidation research symbol") from exc


def _listing_url(instrument_id: str) -> str:
    prefix = f"data/futures/cm/daily/liquidationSnapshot/{instrument_id}/"
    return f"{BINANCE_VISION_BUCKET_URL}?delimiter=/&prefix={prefix}"


def _fetch_bytes(
    url: str,
    *,
    proxy_url: str,
    timeout_seconds: int,
    retry_attempts: int = 6,
) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "s3-ap-northeast-1.amazonaws.com":
        raise ValueError("Binance archive listing must use the allowlisted official HTTPS host")
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    opener = urllib.request.build_opener(*handlers) if proxy_url else urllib.request.build_opener()
    last_error: Exception | None = None
    for attempt in range(retry_attempts):
        try:
            with opener.open(url, timeout=timeout_seconds) as response:
                payload = response.read(5_000_001)
            if not payload or len(payload) > 5_000_000:
                raise ValueError("Binance liquidation listing is empty or too large")
            return payload
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < retry_attempts:
                time.sleep(min(0.25 * (2**attempt), 2.0))
    assert last_error is not None
    raise last_error


def _parse_listing(raw: bytes, *, instrument_id: str) -> set[date]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Binance liquidation listing is not valid XML") from exc
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    expected_prefix = f"data/futures/cm/daily/liquidationSnapshot/{instrument_id}/"
    if root.findtext("s3:Name", namespaces=namespace) != "data.binance.vision":
        raise ValueError("Binance liquidation listing bucket is not official")
    if root.findtext("s3:Prefix", namespaces=namespace) != expected_prefix:
        raise ValueError("Binance liquidation listing prefix does not match the instrument")
    if root.findtext("s3:IsTruncated", namespaces=namespace) != "false":
        raise ValueError("Binance liquidation listing is paginated or incomplete")
    pattern = re.compile(
        rf"^{re.escape(expected_prefix + instrument_id)}-liquidationSnapshot-"
        r"(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
    )
    zip_days: set[date] = set()
    checksum_days: set[date] = set()
    for node in root.findall("s3:Contents", namespace):
        key = node.findtext("s3:Key", namespaces=namespace) or ""
        is_checksum = key.endswith(".CHECKSUM")
        candidate = key.removesuffix(".CHECKSUM") if is_checksum else key
        match = pattern.fullmatch(candidate)
        if match:
            target = checksum_days if is_checksum else zip_days
            target.add(date.fromisoformat(match.group("day")))
    if zip_days != checksum_days:
        raise ValueError("Binance liquidation archives and CHECKSUM files do not match")
    return zip_days


def _archive_url(instrument_id: str, day: date) -> str:
    filename = f"{instrument_id}-liquidationSnapshot-{day.isoformat()}.zip"
    return f"{BINANCE_COIN_M_LIQUIDATION_ROOT}/{instrument_id}/{filename}"


def _safe_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _parse_archive(
    archive: Path,
    *,
    research_symbol: str,
    instrument_id: str,
    expected_day: date,
) -> tuple[int, list[tuple]]:
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Binance liquidation ZIP failed CRC validation")
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("Binance liquidation archive must contain exactly one CSV")
        expected_name = f"{instrument_id}-liquidationSnapshot-{expected_day.isoformat()}.csv"
        if csv_names[0] != expected_name:
            raise ValueError("Binance liquidation CSV filename does not match the requested day")
        with bundle.open(csv_names[0]) as raw:
            rows = csv.reader(line.decode("utf-8") for line in raw)
            header = next(rows, None)
            if header != _HEADER:
                raise ValueError("Binance liquidation CSV schema does not match the contract")
            observed: list[tuple] = []
            raw_count = 0
            for row in rows:
                raw_count += 1
                if len(row) != len(_HEADER):
                    raise ValueError("Binance liquidation CSV row width is invalid")
                timestamp_ms = int(row[0])
                event_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
                if event_time.date() != expected_day:
                    raise ValueError("Binance liquidation event is outside its archive day")
                side = row[1]
                if side not in {"BUY", "SELL"}:
                    raise ValueError("Binance liquidation side is invalid")
                values = tuple(Decimal(value) for value in row[4:7] + row[8:10])
                if values[0] <= 0 or any(value < 0 or not value.is_finite() for value in values):
                    raise ValueError("Binance liquidation quantities or prices are invalid")
                observed.append(
                    (
                        research_symbol,
                        instrument_id,
                        timestamp_ms,
                        side,
                        row[2],
                        row[3],
                        *values,
                        row[7],
                    )
                )
    return raw_count, sorted(set(observed), key=lambda item: (item[2], item[3], item[6]))


def _aggregate_flow(events: list[tuple]) -> list[CoinMLiquidationFlowBar]:
    buckets: dict[datetime, dict[str, Decimal | int | str]] = defaultdict(
        lambda: {"long": Decimal("0"), "short": Decimal("0"), "count": 0}
    )
    for event in events:
        research_symbol, instrument_id, timestamp_ms, side = event[:4]
        accumulated = event[10]
        event_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        interval_start = event_time.replace(minute=0, second=0, microsecond=0)
        bucket = buckets[interval_start]
        bucket["research_symbol"] = research_symbol
        bucket["instrument_id"] = instrument_id
        # A SELL forced order closes a long; BUY closes a short.
        key = "long" if side == "SELL" else "short"
        bucket[key] = Decimal(bucket[key]) + accumulated
        bucket["count"] = int(bucket["count"]) + 1
    return [
        CoinMLiquidationFlowBar(
            research_symbol=str(bucket["research_symbol"]),
            source_instrument_id=str(bucket["instrument_id"]),
            interval_start=interval_start,
            available_at=interval_start + timedelta(hours=1),
            long_liquidated_contracts=Decimal(bucket["long"]),
            short_liquidated_contracts=Decimal(bucket["short"]),
            unique_event_count=int(bucket["count"]),
        )
        for interval_start, bucket in sorted(buckets.items())
    ]


def summarize_coinm_liquidation_flow(
    bars: list[CoinMLiquidationFlowBar],
    *,
    research_symbol: str,
    as_of: datetime,
    lookback_hours: int = 24,
) -> CoinMLiquidationFlowSnapshot:
    """Return a prefix-only forced-flow snapshot available at ``as_of``."""

    instrument_id = _validated_instrument(research_symbol)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("liquidation flow as_of must be timezone-aware")
    if lookback_hours < 1:
        raise ValueError("liquidation flow lookback must be positive")
    start = as_of - timedelta(hours=lookback_hours)
    eligible = [
        bar
        for bar in bars
        if bar.research_symbol == research_symbol
        and bar.source_instrument_id == instrument_id
        and bar.available_at <= as_of
        and bar.available_at > start
    ]
    long_contracts = sum(
        (bar.long_liquidated_contracts for bar in eligible),
        Decimal("0"),
    )
    short_contracts = sum(
        (bar.short_liquidated_contracts for bar in eligible),
        Decimal("0"),
    )
    total = long_contracts + short_contracts
    imbalance = (
        (short_contracts - long_contracts) / total if total > 0 else Decimal("0")
    )
    return CoinMLiquidationFlowSnapshot(
        research_symbol=research_symbol,
        source_instrument_id=instrument_id,
        as_of=as_of,
        lookback_hours=lookback_hours,
        observed_bar_count=len(eligible),
        unique_event_count=sum(bar.unique_event_count for bar in eligible),
        long_liquidated_contracts=long_contracts,
        short_liquidated_contracts=short_contracts,
        short_minus_long_imbalance=imbalance,
    )


def load_verified_coinm_liquidation_flow(
    manifest: CoinMLiquidationArchiveManifest,
    *,
    workspace_root: Path,
) -> list[CoinMLiquidationFlowBar]:
    verification = verify_binance_coinm_liquidation_manifest(
        manifest,
        workspace_root=workspace_root,
    )
    if not verification["verified"]:
        raise ValueError("COIN-M liquidation archive manifest failed integrity verification")
    payload = json.loads((workspace_root / manifest.flow_path).read_text(encoding="utf-8"))
    return [CoinMLiquidationFlowBar.model_validate(item) for item in payload]


def coinm_bars_to_research_points(
    bars: list[CoinMLiquidationFlowBar],
    *,
    research_symbol: str,
    start: datetime,
    end: datetime,
) -> list[LiquidationFlowPoint]:
    instrument_id = _validated_instrument(research_symbol)
    observed = {
        bar.available_at: bar
        for bar in bars
        if bar.research_symbol == research_symbol
        and bar.source_instrument_id == instrument_id
        and start <= bar.available_at <= end
    }
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(hours=1)
    points: list[LiquidationFlowPoint] = []
    while cursor <= end:
        bar = observed.get(cursor)
        points.append(
            LiquidationFlowPoint(
                timestamp=cursor,
                interval_start=cursor - timedelta(hours=1),
                long_liquidated_contracts=(
                    bar.long_liquidated_contracts if bar else Decimal("0")
                ),
                short_liquidated_contracts=(
                    bar.short_liquidated_contracts if bar else Decimal("0")
                ),
                unique_event_count=bar.unique_event_count if bar else 0,
                source_market_type=(
                    bar.source_market_type if bar else "coin_m_inverse_perpetual"
                ),
                source_instrument_id=instrument_id,
            )
        )
        cursor += timedelta(hours=1)
    if any(
        previous.timestamp >= current.timestamp
        for previous, current in zip(points, points[1:], strict=False)
    ):
        raise ValueError("COIN-M liquidation research points are not strictly ordered")
    return points


def import_binance_coinm_liquidation_history(
    research_symbol: str,
    start_day: date,
    end_day: date,
    *,
    workspace_root: Path,
    proxy_url: str = "",
    timeout_seconds: int = 120,
    workers: int = 8,
) -> CoinMLiquidationArchiveManifest:
    """Import one complete official daily archive window as cross-contract context."""

    instrument_id = _validated_instrument(research_symbol)
    if end_day < start_day or (end_day - start_day).days + 1 < 90:
        raise ValueError("liquidation archive import requires at least 90 calendar days")
    listing_url = _listing_url(instrument_id)
    raw_listing = _fetch_bytes(
        listing_url,
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
    )
    available = _parse_listing(raw_listing, instrument_id=instrument_id)
    requested = [
        start_day + timedelta(days=index)
        for index in range((end_day - start_day).days + 1)
    ]
    missing = [day for day in requested if day not in available]
    if missing:
        raise ValueError(
            "Binance liquidation archive has missing calendar days: "
            + ",".join(day.isoformat() for day in missing[:10])
        )
    archive_root = (
        workspace_root
        / "data"
        / "archive"
        / "binance-coinm-liquidations"
        / instrument_id
    )

    def download(day: date) -> tuple[date, Path, str]:
        url = _archive_url(instrument_id, day)
        parsed = urllib.parse.urlparse(url)
        expected_path = f"/data/futures/cm/daily/liquidationSnapshot/{instrument_id}/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "data.binance.vision"
            or not parsed.path.startswith(expected_path)
        ):
            raise ValueError("Binance liquidation archive URL is outside the official scope")
        path, digest = download_verified_archive(
            url,
            archive_root,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
        )
        return day, path, digest

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        downloads = list(executor.map(download, requested))

    all_events: list[tuple] = []
    day_evidence: list[CoinMLiquidationArchiveDay] = []
    raw_total = 0
    for day, archive, digest in sorted(downloads):
        raw_count, events = _parse_archive(
            archive,
            research_symbol=research_symbol,
            instrument_id=instrument_id,
            expected_day=day,
        )
        raw_total += raw_count
        all_events.extend(events)
        day_evidence.append(
            CoinMLiquidationArchiveDay(
                day=day,
                research_symbol=research_symbol,
                source_instrument_id=instrument_id,
                archive_url=_archive_url(instrument_id, day),
                archive_path=_safe_relative(archive, workspace_root),
                archive_sha256=digest,
                archive_size_bytes=archive.stat().st_size,
                raw_row_count=raw_count,
                unique_event_count=len(events),
            )
        )
    unique_events = sorted(set(all_events), key=lambda item: (item[2], item[3], item[6]))
    bars = _aggregate_flow(unique_events)
    output_root = workspace_root / "output" / "data-ingestion"
    output_root.mkdir(parents=True, exist_ok=True)
    listing_path = output_root / f"binance-coinm-liquidation-listing-{instrument_id}.xml"
    listing_path.write_bytes(raw_listing)
    flow_path = output_root / f"binance-coinm-liquidation-flow-{instrument_id}.json"
    flow_bytes = json.dumps(
        [bar.model_dump(mode="json") for bar in bars],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    flow_path.write_bytes(flow_bytes)
    provisional = CoinMLiquidationArchiveManifest(
        id="pending",
        source_listing_url=listing_url,
        source_listing_path=_safe_relative(listing_path, workspace_root),
        source_listing_sha256=hashlib.sha256(raw_listing).hexdigest(),
        research_symbol=research_symbol,
        source_instrument_id=instrument_id,
        coverage_start=datetime.combine(start_day, datetime.min.time(), tzinfo=UTC),
        coverage_end=datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        expected_calendar_days=len(requested),
        archive_day_count=len(day_evidence),
        raw_row_count=raw_total,
        unique_event_count=len(unique_events),
        duplicate_row_count=raw_total - len(unique_events),
        flow_bar_count=len(bars),
        flow_path=_safe_relative(flow_path, workspace_root),
        flow_sha256=hashlib.sha256(flow_bytes).hexdigest(),
        days=day_evidence,
        limitations=[
            "Source instruments are Binance COIN-M inverse perpetuals, not USDT perpetuals.",
            "The normalized research symbol identifies underlying context only; exact-contract "
            "execution and liquidation claims are prohibited.",
            "Quantities remain exchange-published contract counts because historical contract "
            "notional metadata is not embedded in these archives.",
            "Hourly flow becomes available only after the hour closes.",
        ],
    )
    return provisional.model_copy(update={"id": f"coinm_liq_{provisional.fingerprint}"})


def verify_binance_coinm_liquidation_manifest(
    manifest: CoinMLiquidationArchiveManifest,
    *,
    workspace_root: Path,
) -> dict[str, bool | int | str]:
    fingerprint_ok = manifest.id == f"coinm_liq_{manifest.fingerprint}"
    listing_ok = False
    try:
        listing_path = (workspace_root / manifest.source_listing_path).resolve()
        listing_path.relative_to(workspace_root.resolve())
        listing_bytes = listing_path.read_bytes()
        listing_days = _parse_listing(
            listing_bytes,
            instrument_id=manifest.source_instrument_id,
        )
        listing_ok = (
            hashlib.sha256(listing_bytes).hexdigest() == manifest.source_listing_sha256
            and all(item.day in listing_days for item in manifest.days)
        )
    except (ValueError, OSError):
        listing_ok = False
    archives_ok = True
    observed_raw = 0
    observed_unique = 0
    observed_events: list[tuple] = []
    for item in manifest.days:
        path = (workspace_root / item.archive_path).resolve()
        try:
            path.relative_to(workspace_root.resolve())
        except ValueError:
            archives_ok = False
            break
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item.archive_sha256
        ):
            archives_ok = False
            break
        try:
            raw_count, events = _parse_archive(
                path,
                research_symbol=manifest.research_symbol,
                instrument_id=manifest.source_instrument_id,
                expected_day=item.day,
            )
        except (ValueError, OSError, zipfile.BadZipFile):
            archives_ok = False
            break
        if raw_count != item.raw_row_count or len(events) != item.unique_event_count:
            archives_ok = False
            break
        observed_raw += raw_count
        observed_unique += len(events)
        observed_events.extend(events)
    flow_path = (workspace_root / manifest.flow_path).resolve()
    flow_ok = False
    try:
        flow_path.relative_to(workspace_root.resolve())
        flow_bytes = flow_path.read_bytes()
        flow_payload = json.loads(flow_bytes)
        bars = [CoinMLiquidationFlowBar.model_validate(item) for item in flow_payload]
        recomputed = _aggregate_flow(
            sorted(set(observed_events), key=lambda item: (item[2], item[3], item[6]))
        )
        recomputed_bytes = json.dumps(
            [bar.model_dump(mode="json") for bar in recomputed],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        flow_ok = (
            hashlib.sha256(flow_bytes).hexdigest() == manifest.flow_sha256
            and len(bars) == manifest.flow_bar_count
            and all(bar.research_symbol == manifest.research_symbol for bar in bars)
            and flow_bytes == recomputed_bytes
        )
    except (ValueError, OSError, json.JSONDecodeError):
        flow_ok = False
    counts_ok = (
        observed_raw == manifest.raw_row_count
        and observed_unique == manifest.unique_event_count
        and manifest.duplicate_row_count == observed_raw - observed_unique
    )
    verified = fingerprint_ok and listing_ok and archives_ok and flow_ok and counts_ok
    return {
        "verified": verified,
        "fingerprint_matches": fingerprint_ok,
        "listing_verified": listing_ok,
        "archives_verified": archives_ok,
        "flow_verified": flow_ok,
        "counts_match": counts_ok,
        "archive_day_count": manifest.archive_day_count,
        "unique_event_count": manifest.unique_event_count,
        "source_market_type": manifest.source_market_type,
    }
