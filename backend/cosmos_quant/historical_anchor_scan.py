from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, computed_field, model_validator

from .archive_data import binance_monthly_kline_url
from .official_archive_index import OfficialArchiveIndex
from .research_universe import MarketCatalogEntry, catalog_archive_sha256


class HistoricalAnchorObservation(BaseModel):
    market_id: str
    symbol: str
    archive_url: str
    status: Literal["observed", "absent"]
    official_checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    bar_count: int = Field(default=0, ge=0)
    first_bar_at: datetime | None = None
    last_bar_at: datetime | None = None
    monthly_quote_volume: Decimal = Field(default=Decimal("0"), ge=0)
    average_daily_quote_volume: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_status_evidence(self):
        if self.status == "observed":
            if (
                not self.official_checksum_sha256
                or self.payload_sha256 != self.official_checksum_sha256
                or self.bar_count < 1
                or self.first_bar_at is None
                or self.last_bar_at is None
            ):
                raise ValueError("observed anchor market lacks checksum-bound kline evidence")
        elif any(
            (
                self.official_checksum_sha256,
                self.payload_sha256,
                self.bar_count,
                self.first_bar_at,
                self.last_bar_at,
                self.monthly_quote_volume,
                self.average_daily_quote_volume,
            )
        ):
            raise ValueError("absent anchor market cannot contain synthetic market observations")
        return self


class HistoricalAnchorScan(BaseModel):
    version: int = 1
    id: str
    exchange: Literal["binance"] = "binance"
    market_type: Literal["swap"] = "swap"
    official_archive_index_id: str
    official_archive_index_fingerprint: str = Field(pattern=r"^[0-9a-f]{24}$")
    official_archive_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor_month: date
    source_root: str = "https://data.binance.vision/data/futures/um/monthly/klines/"
    candidate_count: int = Field(ge=1)
    checked_count: int = Field(ge=1)
    observed_count: int = Field(ge=0)
    absent_count: int = Field(ge=0)
    observations: list[HistoricalAnchorObservation]
    catalog_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete_candidate_scan: bool
    historical_point_in_time_complete: bool = False
    limitations: list[str] = Field(default_factory=list)
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
        ).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    @model_validator(mode="after")
    def validate_counts(self):
        market_ids = [item.market_id for item in self.observations]
        if len(market_ids) != len(set(market_ids)):
            raise ValueError("historical anchor scan contains duplicate market identifiers")
        observed = sum(item.status == "observed" for item in self.observations)
        absent = sum(item.status == "absent" for item in self.observations)
        if (
            self.candidate_count != len(self.observations)
            or self.checked_count != len(self.observations)
            or self.observed_count != observed
            or self.absent_count != absent
            or self.checked_count != self.observed_count + self.absent_count
            or self.complete_candidate_scan is not True
        ):
            raise ValueError("historical anchor scan coverage counts are inconsistent")
        # Archive availability alone cannot distinguish a genuine inactive market
        # from an exchange-side archive omission. Keep the stronger claim closed.
        if self.historical_point_in_time_complete:
            raise ValueError(
                "archive-presence scans cannot alone certify listing-state completeness"
            )
        return self

    def market_catalog(self) -> list[MarketCatalogEntry]:
        entries = [
            MarketCatalogEntry(
                exchange=self.exchange,
                market_id=item.market_id,
                symbol=item.symbol,
                base=item.symbol.split("/", 1)[0],
                quote="USDT",
                settle="USDT",
                market_type="swap",
                active=True,
                contract=True,
                linear=True,
                inverse=False,
                status="archive_observed",
                quote_volume_24h=item.average_daily_quote_volume,
                asset_class="unknown",
                catalog_source="official-anchor-scan",
            )
            for item in self.observations
            if item.status == "observed"
        ]
        expected = catalog_archive_sha256(entries)
        if expected != self.catalog_payload_sha256:
            raise ValueError("historical anchor catalog hash mismatch")
        return entries


ArchiveFetcher = Callable[[str], tuple[bytes, str] | None]


def _validated_binance_archive_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "data.binance.vision"
        or not parsed.path.startswith("/data/futures/um/monthly/klines/")
        or not parsed.path.endswith(".zip")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("historical anchor archive URL is outside the official Binance scope")


def _default_archive_fetcher(
    *,
    proxy_url: str,
    timeout_seconds: int,
    maximum_bytes: int,
) -> ArchiveFetcher:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )

    def fetch(url: str) -> tuple[bytes, str] | None:
        _validated_binance_archive_url(url)
        try:
            with opener.open(f"{url}.CHECKSUM", timeout=timeout_seconds) as response:
                checksum = response.read(1024).decode("ascii").split()[0].lower()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if len(checksum) != 64 or any(
            character not in "0123456789abcdef" for character in checksum
        ):
            raise ValueError("official anchor checksum is not a SHA-256 digest")
        with opener.open(url, timeout=timeout_seconds) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > maximum_bytes:
                raise ValueError("historical anchor archive exceeds the response-size limit")
            raw = response.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ValueError("historical anchor archive exceeds the response-size limit")
        observed = hashlib.sha256(raw).hexdigest()
        if observed != checksum:
            raise ValueError("historical anchor archive checksum mismatch")
        return raw, checksum

    return fetch


def _month_bounds(anchor_month: date) -> tuple[datetime, datetime, int]:
    if anchor_month.day != 1:
        raise ValueError("historical anchor month must be the first calendar day")
    start = datetime(anchor_month.year, anchor_month.month, 1, tzinfo=UTC)
    if anchor_month.month == 12:
        following = datetime(anchor_month.year + 1, 1, 1, tzinfo=UTC)
    else:
        following = datetime(anchor_month.year, anchor_month.month + 1, 1, tzinfo=UTC)
    return start, following, (following - start).days


def _parse_monthly_daily_kline(
    raw: bytes,
    *,
    month_start: datetime,
    month_end: datetime,
) -> tuple[int, datetime, datetime, Decimal]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("historical anchor payload is not a ZIP archive") from exc
    with bundle:
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("historical anchor archive must contain exactly one CSV")
        rows: list[tuple[datetime, Decimal]] = []
        with bundle.open(csv_names[0]) as stream:
            reader = csv.reader(line.decode("utf-8") for line in stream)
            for row in reader:
                if not row or row[0] in {"open_time", "timestamp"}:
                    continue
                if len(row) < 9:
                    raise ValueError("historical anchor kline row is incomplete")
                timestamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                if not month_start <= timestamp < month_end:
                    raise ValueError(
                        "historical anchor archive leaks observations outside its month"
                    )
                quote_volume = Decimal(row[7])
                trade_count = int(row[8])
                if quote_volume < 0 or trade_count < 0:
                    raise ValueError("historical anchor kline contains invalid volume or trades")
                rows.append((timestamp, quote_volume))
    if not rows:
        raise ValueError("observed historical anchor archive contains no daily bars")
    timestamps = [item[0] for item in rows]
    if timestamps != sorted(set(timestamps)):
        raise ValueError("historical anchor bars are duplicated or out of order")
    return len(rows), timestamps[0], timestamps[-1], sum((item[1] for item in rows), Decimal("0"))


def build_binance_historical_anchor_scan(
    archive_index: OfficialArchiveIndex,
    *,
    anchor_month: date,
    proxy_url: str = "",
    timeout_seconds: int = 60,
    maximum_archive_bytes: int = 2_000_000,
    maximum_workers: int = 24,
    fetcher: ArchiveFetcher | None = None,
) -> HistoricalAnchorScan:
    if archive_index.exchange != "binance" or archive_index.market_type != "swap":
        raise ValueError("historical anchor scanning currently supports Binance USDT swaps")
    if not archive_index.source_listing_complete:
        raise ValueError("historical anchor scan requires a complete official candidate directory")
    start, end, calendar_days = _month_bounds(anchor_month)
    if end > datetime.now(UTC):
        raise ValueError("historical anchor month must be fully closed")
    effective_fetcher = fetcher or _default_archive_fetcher(
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_archive_bytes,
    )

    def scan_one(market_id: str, symbol: str) -> HistoricalAnchorObservation:
        url = binance_monthly_kline_url(symbol, "1d", anchor_month)
        _validated_binance_archive_url(url)
        result = effective_fetcher(url)
        if result is None:
            return HistoricalAnchorObservation(
                market_id=market_id,
                symbol=symbol,
                archive_url=url,
                status="absent",
            )
        raw, official_checksum = result
        observed_hash = hashlib.sha256(raw).hexdigest()
        if observed_hash != official_checksum:
            raise ValueError(f"{market_id}: historical anchor checksum mismatch")
        count, first, last, quote_volume = _parse_monthly_daily_kline(
            raw,
            month_start=start,
            month_end=end,
        )
        return HistoricalAnchorObservation(
            market_id=market_id,
            symbol=symbol,
            archive_url=url,
            status="observed",
            official_checksum_sha256=official_checksum,
            payload_sha256=observed_hash,
            bar_count=count,
            first_bar_at=first,
            last_bar_at=last,
            monthly_quote_volume=quote_volume,
            average_daily_quote_volume=quote_volume / Decimal(calendar_days),
        )

    observations: list[HistoricalAnchorObservation] = []
    with ThreadPoolExecutor(max_workers=min(max(maximum_workers, 1), 64)) as executor:
        futures = {
            executor.submit(scan_one, item.market_id, item.symbol): item.market_id
            for item in archive_index.instruments
        }
        for future in as_completed(futures):
            try:
                observations.append(future.result())
            except Exception as exc:
                raise RuntimeError(
                    f"historical anchor scan failed for {futures[future]}: {exc}"
                ) from exc
    observations.sort(key=lambda item: item.market_id)
    observed_count = sum(item.status == "observed" for item in observations)
    provisional = HistoricalAnchorScan(
        id="pending",
        official_archive_index_id=archive_index.id,
        official_archive_index_fingerprint=archive_index.fingerprint,
        official_archive_source_sha256=archive_index.source_payload_sha256,
        anchor_month=anchor_month,
        candidate_count=len(archive_index.instruments),
        checked_count=len(observations),
        observed_count=observed_count,
        absent_count=len(observations) - observed_count,
        observations=observations,
        catalog_payload_sha256="0" * 64,
        complete_candidate_scan=True,
        historical_point_in_time_complete=False,
        limitations=[
            "The scan checks every historical directory candidate for checksum-bound daily "
            "klines in the anchor month.",
            "A missing official kline archive is not equivalent to an exchange-published "
            "inactive listing record, so listing-state completeness remains unproven.",
        ],
    )
    entries = [
        MarketCatalogEntry(
            exchange="binance",
            market_id=item.market_id,
            symbol=item.symbol,
            base=item.symbol.split("/", 1)[0],
            quote="USDT",
            settle="USDT",
            market_type="swap",
            active=True,
            contract=True,
            linear=True,
            inverse=False,
            status="archive_observed",
            quote_volume_24h=item.average_daily_quote_volume,
            asset_class="unknown",
            catalog_source="official-anchor-scan",
        )
        for item in observations
        if item.status == "observed"
    ]
    catalog_hash = catalog_archive_sha256(entries)
    with_hash = provisional.model_copy(update={"catalog_payload_sha256": catalog_hash})
    return with_hash.model_copy(update={"id": f"anchor_scan_{with_hash.fingerprint}"})


def save_historical_anchor_manifest(scan: HistoricalAnchorScan, destination: Path) -> Path:
    """Write a deterministic manifest without retaining exchange ZIP payloads."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.partial")
    temporary.write_text(scan.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination
