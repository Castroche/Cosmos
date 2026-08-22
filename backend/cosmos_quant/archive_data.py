from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .domain import FundingRatePoint, OpenInterestPoint, OrderFlowBar, PositioningPoint
from .timeframes import TIMEFRAME_SECONDS

BINANCE_ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um"
OKX_HISTORY_LINK_ENDPOINT = (
    "https://www.okx.com/priapi/v5/broker/public/trade-data/download-link"
)
OKX_FUNDING_ARCHIVE_PATH = "/cdn/okex/traderecords/swaprates/monthly/"
OKX_FUNDING_ARCHIVE_EARLIEST = datetime(2022, 3, 1, tzinfo=UTC)
_OKX_FUNDING_FILENAME = re.compile(
    r"^(?P<instrument>[A-Z0-9_-]+-SWAP)-fundingrates-(?P<month>\d{4}-\d{2})\.zip$"
)


class ArchiveChecksumMismatch(ValueError):
    """A complete HTTP attempt returned bytes that fail the official digest."""


@dataclass(frozen=True)
class OkxFundingArchiveDescriptor:
    instrument_family: str
    instrument_id: str
    month: str
    filename: str
    url: str
    response_sha256: str


@dataclass(frozen=True)
class OkxFundingArchiveEvidence:
    descriptor: OkxFundingArchiveDescriptor
    archive_sha256: str
    archive_size_bytes: int

    def manifest(self) -> dict[str, str | int]:
        payload = asdict(self.descriptor)
        payload.update(
            {
                "archive_sha256": self.archive_sha256,
                "archive_size_bytes": self.archive_size_bytes,
            }
        )
        return payload


@dataclass(frozen=True)
class AggregateTrade:
    aggregate_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    timestamp: datetime
    buyer_is_maker: bool


@dataclass(frozen=True)
class BinanceFuturesMetric:
    timestamp: datetime
    symbol: str
    open_interest_amount: Decimal
    open_interest_value: Decimal
    global_long_short_ratio: Decimal | None
    top_position_long_short_ratio: Decimal | None
    top_account_long_short_ratio: Decimal | None
    taker_long_short_volume_ratio: Decimal | None


def binance_daily_aggtrade_url(symbol: str, day: date) -> str:
    compact = symbol.upper().replace("/", "").replace("-", "").split(":", 1)[0]
    filename = f"{compact}-aggTrades-{day.isoformat()}.zip"
    return f"{BINANCE_ARCHIVE_ROOT}/daily/aggTrades/{compact}/{filename}"


def binance_monthly_aggtrade_url(symbol: str, month: date) -> str:
    compact = symbol.upper().replace("/", "").replace("-", "").split(":", 1)[0]
    month_label = month.strftime("%Y-%m")
    filename = f"{compact}-aggTrades-{month_label}.zip"
    return f"{BINANCE_ARCHIVE_ROOT}/monthly/aggTrades/{compact}/{filename}"


def binance_daily_metrics_url(symbol: str, day: date) -> str:
    compact = symbol.upper().replace("/", "").replace("-", "").split(":", 1)[0]
    filename = f"{compact}-metrics-{day.isoformat()}.zip"
    return f"{BINANCE_ARCHIVE_ROOT}/daily/metrics/{compact}/{filename}"


def binance_monthly_kline_url(symbol: str, timeframe: str, month: date) -> str:
    compact = symbol.upper().replace("/", "").replace("-", "").split(":", 1)[0]
    month_label = month.strftime("%Y-%m")
    filename = f"{compact}-{timeframe}-{month_label}.zip"
    return f"{BINANCE_ARCHIVE_ROOT}/monthly/klines/{compact}/{timeframe}/{filename}"


def binance_daily_kline_url(symbol: str, timeframe: str, day: date) -> str:
    compact = symbol.upper().replace("/", "").replace("-", "").split(":", 1)[0]
    filename = f"{compact}-{timeframe}-{day.isoformat()}.zip"
    return f"{BINANCE_ARCHIVE_ROOT}/daily/klines/{compact}/{timeframe}/{filename}"


def _opener(proxy_url: str):
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    return urllib.request.build_opener(*handlers) if proxy_url else urllib.request.build_opener()


def _okx_instrument_family(symbol: str) -> str:
    normalized = symbol.upper().split(":", 1)[0].replace("/", "-")
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", normalized):
        raise ValueError("OKX funding archive symbol must be a base/quote pair")
    return normalized


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def _month_starts(start: datetime, end: datetime) -> Iterator[datetime]:
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    final = datetime(end.year, end.month, 1, tzinfo=UTC)
    while cursor <= final:
        yield cursor
        cursor = _next_month(cursor)


def _validate_okx_funding_url(
    url: str,
    *,
    instrument_id: str | None = None,
    month: str | None = None,
) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "static.okx.com":
        raise ValueError("OKX funding archive must use the official static.okx.com HTTPS host")
    if not parsed.path.startswith(OKX_FUNDING_ARCHIVE_PATH):
        raise ValueError("OKX funding archive path is outside the official monthly funding root")
    filename = Path(parsed.path).name
    match = _OKX_FUNDING_FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError("OKX funding archive filename does not match the official schema")
    observed_instrument = match.group("instrument")
    observed_month = match.group("month")
    if instrument_id is not None and observed_instrument != instrument_id:
        raise ValueError("OKX funding archive instrument does not match the request")
    if month is not None and observed_month != month:
        raise ValueError("OKX funding archive month does not match the request")
    return filename, observed_month


def _read_okx_descriptor_response(
    opener,
    request: urllib.request.Request,
    *,
    timeout_seconds: int,
    retry_attempts: int = 6,
) -> tuple[str, bytes]:
    last_error: Exception | None = None
    for attempt in range(retry_attempts):
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return response.geturl(), response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        if attempt + 1 < retry_attempts:
            time.sleep(min(0.5 * (2**attempt), 3.0))
    assert last_error is not None
    raise last_error


def fetch_okx_funding_archive_descriptors(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    proxy_url: str = "",
    timeout_seconds: int = 60,
) -> list[OkxFundingArchiveDescriptor]:
    """Resolve immutable monthly OKX funding files from the official history portal."""

    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if start > end:
        raise ValueError("funding archive start must not be after end")
    family = _okx_instrument_family(symbol)
    instrument_id = f"{family}-SWAP"
    descriptors: dict[str, OkxFundingArchiveDescriptor] = {}
    opener = _opener(proxy_url)
    months = list(_month_starts(max(start, OKX_FUNDING_ARCHIVE_EARLIEST), end))
    # The website documents a six-month range, but UTC month boundaries can
    # cross its presentation-timezone limit by several hours. Five calendar
    # months stays strictly inside the limit while avoiding needless calls.
    for offset in range(0, len(months), 5):
        requested_starts = months[offset : offset + 5]
        month_start = requested_starts[0]
        month_end = _next_month(requested_starts[-1]) - timedelta(milliseconds=1)
        requested_months = {item.strftime("%Y-%m") for item in requested_starts}
        payload = {
            "module": "3",
            "instType": "SWAP",
            "instQueryParam": {"instFamilyList": [family]},
            "dateQuery": {
                "dateAggrType": "monthly",
                "begin": str(int(month_start.timestamp() * 1000)),
                "end": str(int(month_end.timestamp() * 1000)),
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            OKX_HISTORY_LINK_ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "COSMOS-Quant/1.0 official-history-ingestion",
            },
            method="POST",
        )
        response_url, raw = _read_okx_descriptor_response(
            opener,
            request,
            timeout_seconds=timeout_seconds,
        )
        final = urllib.parse.urlparse(response_url)
        if (
            final.scheme != "https"
            or final.hostname != "www.okx.com"
            or final.path != urllib.parse.urlparse(OKX_HISTORY_LINK_ENDPOINT).path
        ):
            raise ValueError(
                "OKX history descriptor request redirected outside its official endpoint"
            )
        response_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("OKX history descriptor response is not valid JSON") from exc
        if not isinstance(document, dict) or str(document.get("code")) != "0":
            raise ValueError("OKX history descriptor endpoint returned a failure")
        data = document.get("data")
        if not isinstance(data, dict):
            raise ValueError("OKX history descriptor response has no data object")
        details = data.get("details")
        if not isinstance(details, list):
            raise ValueError("OKX history descriptor response has no details list")
        for detail in details:
            if not isinstance(detail, dict):
                raise ValueError("OKX history descriptor detail is malformed")
            if detail.get("instType") != "SWAP" or detail.get("instFamily") != family:
                raise ValueError("OKX history descriptor returned a different market")
            groups = detail.get("groupDetails")
            if not isinstance(groups, list):
                raise ValueError("OKX history descriptor group is malformed")
            for group in groups:
                if not isinstance(group, dict):
                    raise ValueError("OKX history descriptor file is malformed")
                url = str(group.get("url") or "")
                filename, observed_month = _validate_okx_funding_url(
                    url,
                    instrument_id=instrument_id,
                )
                if str(group.get("filename") or "") != filename:
                    raise ValueError("OKX funding filename and URL disagree")
                # The website interprets date-picker month boundaries in its
                # presentation timezone and can include one adjacent month.
                # Keep only the explicitly requested UTC month; never widen
                # the research interval because the portal returned more.
                if observed_month not in requested_months:
                    continue
                descriptor = OkxFundingArchiveDescriptor(
                    instrument_family=family,
                    instrument_id=instrument_id,
                    month=observed_month,
                    filename=filename,
                    url=url,
                    response_sha256=response_sha256,
                )
                existing = descriptors.get(url)
                if existing is not None and existing != descriptor:
                    raise ValueError("OKX history endpoint returned conflicting descriptors")
                descriptors[url] = descriptor
    return sorted(descriptors.values(), key=lambda item: (item.month, item.url))


def download_hashed_okx_funding_archive(
    descriptor: OkxFundingArchiveDescriptor,
    destination: Path,
    *,
    proxy_url: str = "",
    timeout_seconds: int = 120,
) -> tuple[Path, str, int]:
    """Download an official OKX archive and bind its exact bytes to a SHA-256 digest."""

    filename, _ = _validate_okx_funding_url(
        descriptor.url,
        instrument_id=descriptor.instrument_id,
        month=descriptor.month,
    )
    if filename != descriptor.filename:
        raise ValueError("OKX funding descriptor filename is inconsistent")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    digest_path = destination / f"{filename}.sha256"
    lock_path = destination / f".{filename}.lock"
    with _exclusive_archive_lock(lock_path, timeout_seconds=timeout_seconds):
        if target.exists() and digest_path.exists():
            expected = digest_path.read_text(encoding="ascii").strip().lower()
            observed = hashlib.sha256()
            with target.open("rb") as cached:
                while chunk := cached.read(1024 * 1024):
                    observed.update(chunk)
            if len(expected) == 64 and observed.hexdigest() == expected:
                with zipfile.ZipFile(target) as bundle:
                    if bundle.testzip() is None:
                        return target, expected, target.stat().st_size
        temporary = destination / f".{filename}.partial"
        digest_temporary = destination / f".{filename}.sha256.partial"
        digest = hashlib.sha256()
        size = 0
        try:
            request = urllib.request.Request(
                descriptor.url,
                headers={"User-Agent": "COSMOS-Quant/1.0 official-history-ingestion"},
            )
            opener = _opener(proxy_url)
            with opener.open(request, timeout=timeout_seconds) as response:
                final_url = response.geturl()
                _validate_okx_funding_url(
                    final_url,
                    instrument_id=descriptor.instrument_id,
                    month=descriptor.month,
                )
                expected_length = response.headers.get("Content-Length")
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
            if expected_length not in (None, "") and size != int(expected_length):
                raise ValueError("OKX funding archive content length is incomplete")
            if size == 0:
                raise ValueError("OKX funding archive is empty")
            with zipfile.ZipFile(temporary) as bundle:
                corrupted = bundle.testzip()
                if corrupted is not None:
                    raise ValueError(f"OKX funding archive has a corrupt member: {corrupted}")
            temporary.replace(target)
            digest_temporary.write_text(digest.hexdigest(), encoding="ascii")
            digest_temporary.replace(digest_path)
        finally:
            if temporary.exists():
                temporary.unlink()
            if digest_temporary.exists():
                digest_temporary.unlink()
        return target, digest.hexdigest(), size


def iter_okx_funding_rates(
    archive: Path,
    *,
    instrument_id: str,
    start: datetime,
    end: datetime,
) -> Iterator[FundingRatePoint]:
    """Parse one official OKX funding ZIP with strict market and ordering checks."""

    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    previous_timestamp: datetime | None = None
    observed_timestamps: set[datetime] = set()
    with zipfile.ZipFile(archive) as bundle:
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("OKX funding archive must contain exactly one CSV")
        with bundle.open(csv_names[0]) as raw:
            rows = csv.DictReader(line.decode("utf-8-sig") for line in raw)
            required = {"instrument_name", "funding_rate", "funding_time"}
            if not rows.fieldnames or set(rows.fieldnames) != required:
                raise ValueError("OKX funding CSV does not match the official schema")
            for row in rows:
                if row["instrument_name"] != instrument_id:
                    raise ValueError("OKX funding row belongs to a different instrument")
                try:
                    timestamp = datetime.fromtimestamp(int(row["funding_time"]) / 1000, tz=UTC)
                    rate = Decimal(row["funding_rate"])
                except (ValueError, ArithmeticError) as exc:
                    raise ValueError("OKX funding row contains an invalid value") from exc
                if not rate.is_finite() or abs(rate) > 1:
                    raise ValueError("OKX funding rate is outside a defensible numeric range")
                if previous_timestamp is not None and timestamp <= previous_timestamp:
                    raise ValueError("OKX funding rows must be strictly increasing and unique")
                if timestamp in observed_timestamps:
                    raise ValueError("OKX funding archive contains duplicate timestamps")
                previous_timestamp = timestamp
                observed_timestamps.add(timestamp)
                if start <= timestamp <= end:
                    yield FundingRatePoint(timestamp=timestamp, rate=rate)


def fetch_okx_funding_archive_history(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    destination: Path,
    proxy_url: str = "",
    timeout_seconds: int = 120,
) -> tuple[list[FundingRatePoint], list[OkxFundingArchiveEvidence]]:
    """Fetch, hash, validate and combine official monthly OKX funding archives."""

    descriptors = fetch_okx_funding_archive_descriptors(
        symbol,
        start,
        end,
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
    )
    points: dict[datetime, FundingRatePoint] = {}
    evidence: list[OkxFundingArchiveEvidence] = []
    for descriptor in descriptors:
        archive, archive_sha256, size = download_hashed_okx_funding_archive(
            descriptor,
            destination,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
        )
        for point in iter_okx_funding_rates(
            archive,
            instrument_id=descriptor.instrument_id,
            start=start,
            end=end,
        ):
            existing = points.get(point.timestamp)
            if existing is not None and existing != point:
                raise ValueError("OKX monthly funding archives disagree at the same timestamp")
            points[point.timestamp] = point
        evidence.append(
            OkxFundingArchiveEvidence(
                descriptor=descriptor,
                archive_sha256=archive_sha256,
                archive_size_bytes=size,
            )
        )
    return [points[key] for key in sorted(points)], evidence


def download_verified_archive(
    url: str,
    destination: Path,
    *,
    proxy_url: str = "",
    timeout_seconds: int = 120,
    retry_attempts: int = 8,
) -> tuple[Path, str]:
    """Download one official archive and reject it unless its CHECKSUM matches."""

    if retry_attempts < 1:
        raise ValueError("retry_attempts must be positive")
    last_error: Exception | None = None
    for attempt in range(retry_attempts):
        try:
            return _download_verified_archive_once(
                url,
                destination,
                proxy_url=proxy_url,
                timeout_seconds=timeout_seconds,
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ArchiveChecksumMismatch,
        ) as exc:
            last_error = exc
            if attempt + 1 < retry_attempts:
                time.sleep(min(0.25 * (2**attempt), 2.0))
    assert last_error is not None
    raise last_error


@contextmanager
def _exclusive_archive_lock(lock_path: Path, *, timeout_seconds: int):
    """Serialize a cache target across threads and independent Agent processes."""

    deadline = time.monotonic() + timeout_seconds + 30
    stale_after = timeout_seconds + 30
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_after:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for archive lock: {lock_path}") from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            lock_path.unlink()


def _download_verified_archive_once(
    url: str,
    destination: Path,
    *,
    proxy_url: str,
    timeout_seconds: int,
) -> tuple[Path, str]:

    destination.mkdir(parents=True, exist_ok=True)
    filename = url.rsplit("/", 1)[-1]
    target = destination / filename
    lock_path = destination / f".{filename}.lock"
    with _exclusive_archive_lock(lock_path, timeout_seconds=timeout_seconds):
        checksum_url = f"{url}.CHECKSUM"
        opener = _opener(proxy_url)
        with opener.open(checksum_url, timeout=timeout_seconds) as response:
            checksum_text = response.read().decode("utf-8").strip()
        expected = checksum_text.split()[0].lower()
        if len(expected) != 64:
            raise ValueError("archive CHECKSUM does not contain a SHA-256 digest")
        if target.exists():
            cached_digest = hashlib.sha256()
            with target.open("rb") as cached:
                while chunk := cached.read(1024 * 1024):
                    cached_digest.update(chunk)
            if cached_digest.hexdigest() == expected:
                return target, expected
        temporary = destination / f".{filename}.partial"
        digest = hashlib.sha256()
        try:
            with (
                opener.open(url, timeout=timeout_seconds) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                raise ArchiveChecksumMismatch(
                    f"archive checksum mismatch: expected {expected}, received {actual}"
                )
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target, expected


def iter_binance_aggregate_trades(archive: Path) -> Iterator[AggregateTrade]:
    with zipfile.ZipFile(archive) as bundle:
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("aggregate-trade archive must contain exactly one CSV")
        with bundle.open(csv_names[0]) as raw:
            rows = csv.reader(line.decode("utf-8") for line in raw)
            for row in rows:
                if not row or row[0] in {"agg_trade_id", "a"}:
                    continue
                if len(row) < 7:
                    raise ValueError("aggregate-trade row has fewer than seven columns")
                timestamp_ms = int(row[5])
                yield AggregateTrade(
                    aggregate_id=int(row[0]),
                    price=Decimal(row[1]),
                    quantity=Decimal(row[2]),
                    first_trade_id=int(row[3]),
                    last_trade_id=int(row[4]),
                    timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                    buyer_is_maker=row[6].strip().lower() == "true",
                )


def iter_binance_futures_metrics(archive: Path) -> Iterator[BinanceFuturesMetric]:
    """Read official point-in-time Binance futures metrics from one daily archive."""

    with zipfile.ZipFile(archive) as bundle:
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("futures-metrics archive must contain exactly one CSV")
        with bundle.open(csv_names[0]) as raw:
            rows = csv.DictReader(line.decode("utf-8") for line in raw)
            required = {
                "create_time",
                "symbol",
                "sum_open_interest",
                "sum_open_interest_value",
                "count_toptrader_long_short_ratio",
                "sum_toptrader_long_short_ratio",
                "count_long_short_ratio",
                "sum_taker_long_short_vol_ratio",
            }
            if not rows.fieldnames or not required.issubset(rows.fieldnames):
                raise ValueError("futures-metrics CSV does not match the official schema")

            def optional_decimal(value: str | None) -> Decimal | None:
                return Decimal(value) if value not in (None, "") else None

            for row in rows:
                timestamp = datetime.strptime(
                    row["create_time"],
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=UTC)
                yield BinanceFuturesMetric(
                    timestamp=timestamp,
                    symbol=row["symbol"],
                    open_interest_amount=Decimal(row["sum_open_interest"]),
                    open_interest_value=Decimal(row["sum_open_interest_value"]),
                    top_account_long_short_ratio=optional_decimal(
                        row["count_toptrader_long_short_ratio"]
                    ),
                    top_position_long_short_ratio=optional_decimal(
                        row["sum_toptrader_long_short_ratio"]
                    ),
                    global_long_short_ratio=optional_decimal(row["count_long_short_ratio"]),
                    taker_long_short_volume_ratio=optional_decimal(
                        row["sum_taker_long_short_vol_ratio"]
                    ),
                )


def metrics_to_research_points(
    metrics: Iterable[BinanceFuturesMetric],
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[OpenInterestPoint], list[PositioningPoint]]:
    """Convert official metrics without copying later observations backward."""

    open_interest: dict[datetime, OpenInterestPoint] = {}
    positioning: dict[datetime, PositioningPoint] = {}
    for item in metrics:
        if not start <= item.timestamp <= end:
            continue
        open_interest[item.timestamp] = OpenInterestPoint(
            timestamp=item.timestamp,
            open_interest_amount=item.open_interest_amount,
            open_interest_value=item.open_interest_value,
        )
        ratio = item.global_long_short_ratio
        if ratio is None or ratio <= 0:
            continue
        long_fraction = ratio / (Decimal("1") + ratio)
        positioning[item.timestamp] = PositioningPoint(
            timestamp=item.timestamp,
            long_short_ratio=ratio,
            long_account_fraction=long_fraction,
            short_account_fraction=Decimal("1") - long_fraction,
            source_variant="binance_metrics_global_account",
        )
    return (
        [open_interest[key] for key in sorted(open_interest)],
        [positioning[key] for key in sorted(positioning)],
    )


def downsample_latest_observation[TimestampedPoint](
    points: Iterable[TimestampedPoint],
    *,
    timeframe: str,
) -> list[TimestampedPoint]:
    """Keep the last causal observation in each bar bucket without shifting timestamps."""

    seconds = TIMEFRAME_SECONDS[timeframe]
    buckets: dict[int, TimestampedPoint] = {}
    for point in sorted(points, key=lambda item: item.timestamp):
        epoch = int(point.timestamp.timestamp())
        buckets[epoch - (epoch % seconds)] = point
    return [buckets[key] for key in sorted(buckets)]


def iter_binance_kline_order_flow(
    archive: Path,
    *,
    start: datetime,
    end: datetime,
) -> Iterator[OrderFlowBar]:
    """Read causal taker buy/sell flow embedded in official closed futures klines."""

    with zipfile.ZipFile(archive) as bundle:
        csv_names = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError("kline archive must contain exactly one CSV")
        with bundle.open(csv_names[0]) as raw:
            rows = csv.reader(line.decode("utf-8") for line in raw)
            for row in rows:
                if not row or row[0] in {"open_time", "timestamp"}:
                    continue
                if len(row) < 11:
                    raise ValueError("kline row has fewer than eleven columns")
                timestamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                if not start <= timestamp <= end:
                    continue
                base_volume = Decimal(row[5])
                quote_volume = Decimal(row[7])
                trade_count = int(row[8])
                taker_buy = Decimal(row[9])
                if base_volume <= 0 or quote_volume <= 0 or trade_count <= 0:
                    continue
                taker_sell = base_volume - taker_buy
                yield OrderFlowBar(
                    timestamp=timestamp,
                    # One exchange-published aggregate kline contains the
                    # underlying trade count but not aggTrade grouping count.
                    aggregate_count=1,
                    underlying_trade_count=trade_count,
                    base_volume=base_volume,
                    quote_volume=quote_volume,
                    taker_buy_base_volume=taker_buy,
                    taker_sell_base_volume=taker_sell,
                    delta_base_volume=taker_buy - taker_sell,
                    maximum_trade_quote=Decimal("0"),
                )


def aggregate_order_flow(
    trades: Iterable[AggregateTrade],
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[OrderFlowBar]:
    seconds = TIMEFRAME_SECONDS[timeframe]
    buckets: dict[int, dict[str, Decimal | int]] = {}
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())
    for trade in trades:
        epoch = int(trade.timestamp.timestamp())
        bucket_epoch = epoch - (epoch % seconds)
        if bucket_epoch < start_epoch or bucket_epoch > end_epoch:
            continue
        bucket = buckets.setdefault(
            bucket_epoch,
            {
                "aggregate_count": 0,
                "underlying_trade_count": 0,
                "base_volume": Decimal("0"),
                "quote_volume": Decimal("0"),
                "buy": Decimal("0"),
                "sell": Decimal("0"),
                "maximum_trade_quote": Decimal("0"),
            },
        )
        quote = trade.price * trade.quantity
        bucket["aggregate_count"] += 1
        bucket["underlying_trade_count"] += trade.last_trade_id - trade.first_trade_id + 1
        bucket["base_volume"] += trade.quantity
        bucket["quote_volume"] += quote
        taker_key = "sell" if trade.buyer_is_maker else "buy"
        bucket[taker_key] += trade.quantity
        bucket["maximum_trade_quote"] = max(bucket["maximum_trade_quote"], quote)
    result = []
    for timestamp in sorted(buckets):
        bucket = buckets[timestamp]
        buy = Decimal(bucket["buy"])
        sell = Decimal(bucket["sell"])
        result.append(
            OrderFlowBar(
                timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
                aggregate_count=int(bucket["aggregate_count"]),
                underlying_trade_count=int(bucket["underlying_trade_count"]),
                base_volume=Decimal(bucket["base_volume"]),
                quote_volume=Decimal(bucket["quote_volume"]),
                taker_buy_base_volume=buy,
                taker_sell_base_volume=sell,
                delta_base_volume=buy - sell,
                maximum_trade_quote=Decimal(bucket["maximum_trade_quote"]),
            )
        )
    return result


def resample_order_flow(
    bars: Iterable[OrderFlowBar],
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[OrderFlowBar]:
    """Aggregate already causal lower-timeframe bars without reopening raw archives."""

    seconds = TIMEFRAME_SECONDS[timeframe]
    buckets: dict[int, list[OrderFlowBar]] = {}
    for bar in bars:
        epoch = int(bar.timestamp.timestamp())
        bucket_epoch = epoch - (epoch % seconds)
        bucket_time = datetime.fromtimestamp(bucket_epoch, tz=UTC)
        if start <= bucket_time <= end:
            buckets.setdefault(bucket_epoch, []).append(bar)
    result = []
    for timestamp in sorted(buckets):
        items = buckets[timestamp]
        buy = sum((item.taker_buy_base_volume for item in items), Decimal("0"))
        sell = sum((item.taker_sell_base_volume for item in items), Decimal("0"))
        result.append(
            OrderFlowBar(
                timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
                aggregate_count=sum(item.aggregate_count for item in items),
                underlying_trade_count=sum(item.underlying_trade_count for item in items),
                base_volume=buy + sell,
                quote_volume=sum((item.quote_volume for item in items), Decimal("0")),
                taker_buy_base_volume=buy,
                taker_sell_base_volume=sell,
                delta_base_volume=buy - sell,
                maximum_trade_quote=max(item.maximum_trade_quote for item in items),
            )
        )
    return result


def copy_archive(source: Path, destination: Path) -> None:
    """Copy an already verified archive into a controlled cache location."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
