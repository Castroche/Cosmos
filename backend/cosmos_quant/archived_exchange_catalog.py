from __future__ import annotations

import base64
import gzip
import hashlib
import json
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse

from pydantic import BaseModel, Field, computed_field, model_validator

from .research_universe import (
    HistoricalCatalogArchiveEvidence,
    MarketCatalogEntry,
    catalog_archive_sha256,
)

BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ARQUIVO_CDX_ROOT = "https://arquivo.pt/wayback/cdx"
ARQUIVO_REPLAY_ROOT = "https://arquivo.pt/noFrame/replay"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
WAYBACK_CDX_ROOT = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY_ROOT = "https://web.archive.org/web"


class ArchivedExchangeCatalogManifest(BaseModel):
    version: int = 1
    id: str
    exchange: Literal["binance"] = "binance"
    market_type: Literal["swap"] = "swap"
    archive_provider: Literal["arquivo_pt", "internet_archive"] = "arquivo_pt"
    original_source_url: str = BINANCE_EXCHANGE_INFO_URL
    archive_index_url: str
    archive_replay_url: str
    archive_capture_at: datetime
    original_server_time: datetime
    capture_lag_seconds: int = Field(ge=0, le=3600)
    archive_digest_sha1_base32: str = Field(pattern=r"^[A-Z2-7]{32}$")
    archive_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_index_path: str
    raw_payload_path: str
    raw_symbol_count: int = Field(ge=1)
    normalized_record_count: int = Field(ge=1)
    status_counts: dict[str, int]
    catalog_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: list[MarketCatalogEntry]
    historical_point_in_time_complete: bool = True
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
    def validate_derived_evidence(self):
        if self.version != 1 or not self.historical_point_in_time_complete:
            raise ValueError("archived exchange catalog must be a complete v1 artifact")
        if self.archive_capture_at.tzinfo is None or self.original_server_time.tzinfo is None:
            raise ValueError("archived exchange catalog times must be timezone-aware")
        expected_lag = int((self.archive_capture_at - self.original_server_time).total_seconds())
        if expected_lag < 0 or self.capture_lag_seconds != expected_lag:
            raise ValueError("archived exchange catalog capture lag is inconsistent")
        if self.normalized_record_count != len(self.entries):
            raise ValueError("archived exchange catalog record count is inconsistent")
        if len({item.market_id for item in self.entries}) != len(self.entries):
            raise ValueError("archived exchange catalog contains duplicate market identifiers")
        observed_status_counts = dict(
            sorted(Counter(item.status or "" for item in self.entries).items())
        )
        if self.status_counts != observed_status_counts:
            raise ValueError("archived exchange catalog status counts are inconsistent")
        if self.catalog_payload_sha256 != catalog_archive_sha256(self.entries):
            raise ValueError("archived exchange catalog payload hash is inconsistent")
        validator = (
            _validate_urls
            if self.archive_provider == "arquivo_pt"
            else _validate_binance_wayback_urls
        )
        validator(
            index_url=self.archive_index_url,
            replay_url=self.archive_replay_url,
            capture_timestamp=self.archive_capture_at.strftime("%Y%m%d%H%M%S"),
        )
        for value in (self.archive_index_path, self.raw_payload_path):
            normalized = value.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise ValueError("archived exchange catalog local path is unsafe")
        return self

    def archive_evidence(self) -> HistoricalCatalogArchiveEvidence:
        return HistoricalCatalogArchiveEvidence(
            exchange=self.exchange,
            market_type=self.market_type,
            archive_as_of=self.original_server_time,
            source_kind="trusted_web_archive_of_official_endpoint",
            source_url=self.archive_replay_url,
            payload_sha256=self.catalog_payload_sha256,
            record_count=self.normalized_record_count,
            archive_provider=self.archive_provider,
            archive_capture_at=self.archive_capture_at,
            original_source_url=self.original_source_url,
            archive_index_url=self.archive_index_url,
            archive_index_sha256=self.archive_index_sha256,
            archive_digest_sha1_base32=self.archive_digest_sha1_base32,
            raw_payload_sha256=self.raw_payload_sha256,
            raw_payload_path=self.raw_payload_path,
            archive_index_path=self.archive_index_path,
            original_server_time=self.original_server_time,
            capture_lag_seconds=self.capture_lag_seconds,
        )


class ArchivedOkxCatalogManifest(BaseModel):
    version: int = 1
    id: str
    exchange: Literal["okx"] = "okx"
    market_type: Literal["swap"] = "swap"
    archive_provider: Literal["internet_archive"] = "internet_archive"
    original_source_url: str = OKX_INSTRUMENTS_URL
    archive_index_url: str
    archive_replay_url: str
    archive_capture_at: datetime
    original_server_time: datetime
    capture_lag_seconds: int = Field(default=0, ge=0, le=3600)
    archive_digest_sha1_base32: str = Field(pattern=r"^[A-Z2-7]{32}$")
    archive_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_index_path: str
    raw_payload_path: str
    raw_symbol_count: int = Field(ge=1)
    normalized_record_count: int = Field(ge=1)
    status_counts: dict[str, int]
    catalog_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: list[MarketCatalogEntry]
    historical_point_in_time_complete: bool = True
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
    def validate_derived_evidence(self):
        if self.version != 1 or not self.historical_point_in_time_complete:
            raise ValueError("archived OKX catalog must be a complete v1 artifact")
        if (
            self.archive_capture_at.tzinfo is None
            or self.original_server_time != self.archive_capture_at
            or self.capture_lag_seconds != 0
        ):
            raise ValueError("archived OKX catalog capture time is inconsistent")
        if self.normalized_record_count != len(self.entries):
            raise ValueError("archived OKX catalog record count is inconsistent")
        if len({item.market_id for item in self.entries}) != len(self.entries):
            raise ValueError("archived OKX catalog contains duplicate market identifiers")
        observed_status_counts = dict(
            sorted(Counter(item.status or "" for item in self.entries).items())
        )
        if self.status_counts != observed_status_counts:
            raise ValueError("archived OKX catalog status counts are inconsistent")
        if self.catalog_payload_sha256 != catalog_archive_sha256(self.entries):
            raise ValueError("archived OKX catalog payload hash is inconsistent")
        _validate_okx_urls(
            index_url=self.archive_index_url,
            replay_url=self.archive_replay_url,
            capture_timestamp=self.archive_capture_at.strftime("%Y%m%d%H%M%S"),
        )
        for value in (self.archive_index_path, self.raw_payload_path):
            normalized = value.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise ValueError("archived OKX catalog local path is unsafe")
        return self

    def archive_evidence(self) -> HistoricalCatalogArchiveEvidence:
        return HistoricalCatalogArchiveEvidence(
            exchange=self.exchange,
            market_type=self.market_type,
            archive_as_of=self.original_server_time,
            source_kind="trusted_web_archive_of_official_endpoint",
            source_url=self.archive_replay_url,
            payload_sha256=self.catalog_payload_sha256,
            record_count=self.normalized_record_count,
            archive_provider=self.archive_provider,
            archive_capture_at=self.archive_capture_at,
            original_source_url=self.original_source_url,
            archive_index_url=self.archive_index_url,
            archive_index_sha256=self.archive_index_sha256,
            archive_digest_sha1_base32=self.archive_digest_sha1_base32,
            raw_payload_sha256=self.raw_payload_sha256,
            raw_payload_path=self.raw_payload_path,
            archive_index_path=self.archive_index_path,
            original_server_time=self.original_server_time,
            capture_lag_seconds=self.capture_lag_seconds,
        )


Fetcher = Callable[[str], bytes]


def _capture_time(timestamp: str) -> datetime:
    if len(timestamp) != 14 or not timestamp.isdigit():
        raise ValueError("archive timestamp must use YYYYMMDDhhmmss")
    try:
        return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("archive timestamp is invalid") from exc


def _index_url(timestamp: str) -> str:
    query = urlencode(
        {
            "url": "fapi.binance.com/fapi/v1/exchangeInfo",
            "from": timestamp,
            "to": timestamp,
            "output": "json",
        }
    )
    return f"{ARQUIVO_CDX_ROOT}?{query}"


def _replay_url(timestamp: str) -> str:
    return f"{ARQUIVO_REPLAY_ROOT}/{timestamp}id_/{BINANCE_EXCHANGE_INFO_URL}"


def _binance_wayback_index_url(timestamp: str) -> str:
    query = urlencode(
        {
            "url": BINANCE_EXCHANGE_INFO_URL,
            "from": timestamp,
            "to": timestamp,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,digest,statuscode,mimetype",
        }
    )
    return f"{WAYBACK_CDX_ROOT}?{query}"


def _binance_wayback_replay_url(timestamp: str) -> str:
    return f"{WAYBACK_REPLAY_ROOT}/{timestamp}id_/{BINANCE_EXCHANGE_INFO_URL}"


def _okx_index_url(timestamp: str) -> str:
    query = urlencode(
        {
            "url": OKX_INSTRUMENTS_URL,
            "from": timestamp,
            "to": timestamp,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,digest,statuscode,mimetype",
        }
    )
    return f"{WAYBACK_CDX_ROOT}?{query}"


def _okx_replay_url(timestamp: str) -> str:
    return f"{WAYBACK_REPLAY_ROOT}/{timestamp}id_/{OKX_INSTRUMENTS_URL}"


def _validate_urls(*, index_url: str, replay_url: str, capture_timestamp: str) -> None:
    index = urlparse(index_url)
    replay = urlparse(replay_url)
    if (
        index.scheme != "https"
        or index.hostname != "arquivo.pt"
        or index.path != "/wayback/cdx"
        or replay.scheme != "https"
        or replay.hostname != "arquivo.pt"
        or replay.path
        != f"/noFrame/replay/{capture_timestamp}id_/https://fapi.binance.com/fapi/v1/exchangeInfo"
        or replay.query
        or replay.fragment
    ):
        raise ValueError("archived exchange catalog URL is outside the allowlisted scope")


def _validate_okx_urls(*, index_url: str, replay_url: str, capture_timestamp: str) -> None:
    index = urlparse(index_url)
    replay = urlparse(replay_url)
    if (
        index.scheme != "https"
        or index.hostname != "web.archive.org"
        or index.path != "/cdx/search/cdx"
        or replay.scheme != "https"
        or replay.hostname != "web.archive.org"
        or replay.path
        != f"/web/{capture_timestamp}id_/https://www.okx.com/api/v5/public/instruments"
        or replay.query != "instType=SWAP"
        or replay.fragment
    ):
        raise ValueError("archived OKX catalog URL is outside the allowlisted scope")


def _validate_binance_wayback_urls(
    *,
    index_url: str,
    replay_url: str,
    capture_timestamp: str,
) -> None:
    index = urlparse(index_url)
    replay = urlparse(replay_url)
    if (
        index.scheme != "https"
        or index.hostname != "web.archive.org"
        or index.path != "/cdx/search/cdx"
        or replay.scheme != "https"
        or replay.hostname != "web.archive.org"
        or replay.path
        != f"/web/{capture_timestamp}id_/https://fapi.binance.com/fapi/v1/exchangeInfo"
        or replay.query
        or replay.fragment
    ):
        raise ValueError("archived Binance catalog URL is outside the allowlisted scope")


def _default_fetcher(*, timeout_seconds: int, maximum_bytes: int) -> Fetcher:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())

    def fetch(url: str) -> bytes:
        with opener.open(url, timeout=timeout_seconds) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > maximum_bytes:
                raise ValueError("archived exchange response exceeds the size limit")
            payload = response.read(maximum_bytes + 1)
        if not payload or len(payload) > maximum_bytes:
            raise ValueError("archived exchange response is empty or too large")
        return payload

    return fetch


def _parse_index(raw: bytes, *, timestamp: str) -> dict:
    try:
        records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Arquivo.pt CDX response is invalid") from exc
    matching = [
        item
        for item in records
        if item.get("timestamp") == timestamp
        and item.get("url") == BINANCE_EXCHANGE_INFO_URL
        and str(item.get("status")) == "200"
        and item.get("mime") == "application/json"
    ]
    if len(matching) != 1:
        raise ValueError("Arquivo.pt CDX response must contain exactly one matching capture")
    digest = str(matching[0].get("digest", ""))
    if len(digest) != 32 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in digest
    ):
        raise ValueError("Arquivo.pt CDX digest is not a SHA-1 base32 value")
    return matching[0]


def _parse_wayback_index(
    raw: bytes,
    *,
    timestamp: str,
    original_source_url: str = OKX_INSTRUMENTS_URL,
) -> dict:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Internet Archive CDX response is invalid") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not all(isinstance(row, list) for row in payload)
    ):
        raise ValueError("Internet Archive CDX response must contain one capture")
    header, values = payload
    if len(header) != len(values) or len(set(header)) != len(header):
        raise ValueError("Internet Archive CDX response columns are invalid")
    record = dict(zip(header, values, strict=True))
    if (
        record.get("timestamp") != timestamp
        or record.get("original") != original_source_url
        or str(record.get("statuscode")) != "200"
        or record.get("mimetype") != "application/json"
    ):
        raise ValueError("Internet Archive CDX capture does not match the official endpoint")
    digest = str(record.get("digest", ""))
    if len(digest) != 32 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in digest
    ):
        raise ValueError("Internet Archive CDX digest is not a SHA-1 base32 value")
    return record


def _timestamp_milliseconds(value: object, *, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Binance archived {field} is invalid")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _parse_binance_exchange_info(raw: bytes) -> tuple[datetime, int, list[MarketCatalogEntry]]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("archived Binance exchangeInfo is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("archived Binance exchangeInfo must be an object")
    if payload.get("timezone") != "UTC" or payload.get("futuresType") != "U_MARGINED":
        raise ValueError("archived Binance exchangeInfo has the wrong market scope")
    server_time = _timestamp_milliseconds(payload.get("serverTime"), field="serverTime")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("archived Binance exchangeInfo contains no symbols")
    raw_ids: list[str] = []
    entries: list[MarketCatalogEntry] = []
    for item in symbols:
        if not isinstance(item, dict):
            raise ValueError("archived Binance symbol record is invalid")
        market_id = item.get("symbol")
        if not isinstance(market_id, str) or not market_id:
            raise ValueError("archived Binance symbol identifier is invalid")
        raw_ids.append(market_id)
        if not (
            item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
            and item.get("marginAsset") == "USDT"
        ):
            continue
        base = item.get("baseAsset")
        status = item.get("status")
        if not isinstance(base, str) or not base or not isinstance(status, str) or not status:
            raise ValueError("archived Binance perpetual record is incomplete")
        listed_at = _timestamp_milliseconds(item.get("onboardDate"), field="onboardDate")
        entries.append(
            MarketCatalogEntry(
                exchange="binance",
                market_id=market_id,
                symbol=f"{base}/USDT",
                base=base,
                quote="USDT",
                settle="USDT",
                market_type="swap",
                active=status == "TRADING",
                contract=True,
                linear=True,
                inverse=False,
                listed_at=listed_at,
                listing_time_source="archived_binance_exchangeInfo.onboardDate",
                status=status,
                asset_class="unknown",
                catalog_source="arquivo_pt_memento_of_binance_exchangeInfo",
            )
        )
    if len(raw_ids) != len(set(raw_ids)):
        raise ValueError("archived Binance exchangeInfo contains duplicate symbols")
    entries.sort(key=lambda item: (item.market_id, item.symbol))
    if not entries:
        raise ValueError("archived Binance exchangeInfo has no USDT perpetuals")
    return server_time, len(symbols), entries


def _with_catalog_source(
    entries: list[MarketCatalogEntry],
    catalog_source: str,
) -> list[MarketCatalogEntry]:
    return [
        entry.model_copy(update={"catalog_source": catalog_source})
        for entry in entries
    ]


def _parse_okx_instruments(raw: bytes) -> tuple[int, list[MarketCatalogEntry]]:
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise ValueError("archived OKX instruments gzip is invalid") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("archived OKX instruments is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise ValueError("archived OKX instruments response was not successful")
    records = payload.get("data")
    if not isinstance(records, list) or not records:
        raise ValueError("archived OKX instruments contains no records")
    raw_ids: list[str] = []
    entries: list[MarketCatalogEntry] = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("archived OKX instrument record is invalid")
        market_id = item.get("instId")
        if not isinstance(market_id, str) or not market_id:
            raise ValueError("archived OKX instrument identifier is invalid")
        raw_ids.append(market_id)
        if not (
            item.get("instType") == "SWAP"
            and item.get("settleCcy") == "USDT"
            and item.get("ctType") == "linear"
            and market_id.endswith("-USDT-SWAP")
        ):
            continue
        base = market_id.removesuffix("-USDT-SWAP")
        state = item.get("state")
        list_time = item.get("listTime")
        if not isinstance(state, str) or not state or not isinstance(list_time, str):
            raise ValueError("archived OKX USDT swap record is incomplete")
        try:
            listed_at = _timestamp_milliseconds(int(list_time), field="listTime")
        except (TypeError, ValueError) as exc:
            raise ValueError("archived OKX listTime is invalid") from exc
        entries.append(
            MarketCatalogEntry(
                exchange="okx",
                market_id=market_id,
                symbol=f"{base}/USDT",
                base=base,
                quote="USDT",
                settle="USDT",
                market_type="swap",
                active=state == "live",
                contract=True,
                linear=True,
                inverse=False,
                listed_at=listed_at,
                listing_time_source="archived_okx_instruments.listTime",
                status=state,
                asset_class="unknown",
                catalog_source="internet_archive_memento_of_okx_instruments",
            )
        )
    if len(raw_ids) != len(set(raw_ids)):
        raise ValueError("archived OKX instruments contains duplicate identifiers")
    entries.sort(key=lambda item: (item.market_id, item.symbol))
    if not entries:
        raise ValueError("archived OKX instruments has no linear USDT swaps")
    return len(records), entries


def _safe_relative(path: Path, workspace_root: Path) -> str:
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def capture_archived_binance_catalog(
    *,
    timestamp: str,
    workspace_root: Path,
    output_root: Path,
    timeout_seconds: int = 60,
    maximum_bytes: int = 2_000_000,
    fetcher: Fetcher | None = None,
    archive_provider: Literal["arquivo_pt", "internet_archive"] = "arquivo_pt",
) -> ArchivedExchangeCatalogManifest:
    capture_at = _capture_time(timestamp)
    index_url = (
        _index_url(timestamp)
        if archive_provider == "arquivo_pt"
        else _binance_wayback_index_url(timestamp)
    )
    replay_url = (
        _replay_url(timestamp)
        if archive_provider == "arquivo_pt"
        else _binance_wayback_replay_url(timestamp)
    )
    validator = (
        _validate_urls
        if archive_provider == "arquivo_pt"
        else _validate_binance_wayback_urls
    )
    validator(
        index_url=index_url,
        replay_url=replay_url,
        capture_timestamp=timestamp,
    )
    effective_fetcher = fetcher or _default_fetcher(
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_bytes,
    )
    raw_index = effective_fetcher(index_url)
    index_record = (
        _parse_index(raw_index, timestamp=timestamp)
        if archive_provider == "arquivo_pt"
        else _parse_wayback_index(
            raw_index,
            timestamp=timestamp,
            original_source_url=BINANCE_EXCHANGE_INFO_URL,
        )
    )
    raw_payload = effective_fetcher(replay_url)
    observed_digest = base64.b32encode(hashlib.sha1(raw_payload).digest()).decode("ascii")
    if observed_digest != index_record["digest"]:
        raise ValueError("archived Binance payload does not match the CDX digest")
    server_time, raw_count, entries = _parse_binance_exchange_info(raw_payload)
    if archive_provider == "internet_archive":
        entries = _with_catalog_source(
            entries,
            "internet_archive_memento_of_binance_exchangeInfo",
        )
    capture_lag = int((capture_at - server_time).total_seconds())
    if capture_lag < 0 or capture_lag > 3600:
        raise ValueError("archived Binance response server time is outside the capture bound")

    output_root.mkdir(parents=True, exist_ok=True)
    stem = f"binance-exchangeInfo-{timestamp}"
    index_path = output_root / (
        f"{stem}.cdx.jsonl"
        if archive_provider == "arquivo_pt"
        else f"{stem}.cdx.json"
    )
    payload_path = output_root / f"{stem}.json"
    for path, raw in ((index_path, raw_index), (payload_path, raw_payload)):
        temporary = path.with_suffix(f"{path.suffix}.partial")
        temporary.write_bytes(raw)
        temporary.replace(path)

    provisional = ArchivedExchangeCatalogManifest(
        id="pending",
        archive_provider=archive_provider,
        archive_index_url=index_url,
        archive_replay_url=replay_url,
        archive_capture_at=capture_at,
        original_server_time=server_time,
        capture_lag_seconds=capture_lag,
        archive_digest_sha1_base32=observed_digest,
        archive_index_sha256=hashlib.sha256(raw_index).hexdigest(),
        raw_payload_sha256=hashlib.sha256(raw_payload).hexdigest(),
        archive_index_path=_safe_relative(index_path, workspace_root),
        raw_payload_path=_safe_relative(payload_path, workspace_root),
        raw_symbol_count=raw_count,
        normalized_record_count=len(entries),
        status_counts=dict(sorted(Counter(item.status or "" for item in entries).items())),
        catalog_payload_sha256=catalog_archive_sha256(entries),
        entries=entries,
    )
    return provisional.model_copy(update={"id": f"archived_catalog_{provisional.fingerprint}"})


def capture_archived_okx_catalog(
    *,
    timestamp: str,
    workspace_root: Path,
    output_root: Path,
    timeout_seconds: int = 60,
    maximum_bytes: int = 2_000_000,
    fetcher: Fetcher | None = None,
) -> ArchivedOkxCatalogManifest:
    capture_at = _capture_time(timestamp)
    index_url = _okx_index_url(timestamp)
    replay_url = _okx_replay_url(timestamp)
    _validate_okx_urls(
        index_url=index_url,
        replay_url=replay_url,
        capture_timestamp=timestamp,
    )
    effective_fetcher = fetcher or _default_fetcher(
        timeout_seconds=timeout_seconds,
        maximum_bytes=maximum_bytes,
    )
    raw_index = effective_fetcher(index_url)
    index_record = _parse_wayback_index(raw_index, timestamp=timestamp)
    raw_payload = effective_fetcher(replay_url)
    observed_digest = base64.b32encode(hashlib.sha1(raw_payload).digest()).decode("ascii")
    if observed_digest != index_record["digest"]:
        raise ValueError("archived OKX payload does not match the CDX digest")
    raw_count, entries = _parse_okx_instruments(raw_payload)

    output_root.mkdir(parents=True, exist_ok=True)
    stem = f"okx-instruments-{timestamp}"
    index_path = output_root / f"{stem}.cdx.json"
    payload_path = output_root / f"{stem}.json.gz"
    for path, raw in ((index_path, raw_index), (payload_path, raw_payload)):
        temporary = path.with_suffix(f"{path.suffix}.partial")
        temporary.write_bytes(raw)
        temporary.replace(path)

    provisional = ArchivedOkxCatalogManifest(
        id="pending",
        archive_index_url=index_url,
        archive_replay_url=replay_url,
        archive_capture_at=capture_at,
        original_server_time=capture_at,
        archive_digest_sha1_base32=observed_digest,
        archive_index_sha256=hashlib.sha256(raw_index).hexdigest(),
        raw_payload_sha256=hashlib.sha256(raw_payload).hexdigest(),
        archive_index_path=_safe_relative(index_path, workspace_root),
        raw_payload_path=_safe_relative(payload_path, workspace_root),
        raw_symbol_count=raw_count,
        normalized_record_count=len(entries),
        status_counts=dict(sorted(Counter(item.status or "" for item in entries).items())),
        catalog_payload_sha256=catalog_archive_sha256(entries),
        entries=entries,
    )
    return provisional.model_copy(update={"id": f"archived_okx_catalog_{provisional.fingerprint}"})


def verify_archived_binance_catalog(
    manifest: ArchivedExchangeCatalogManifest,
    *,
    workspace_root: Path,
) -> dict[str, object]:
    if manifest.id != f"archived_catalog_{manifest.fingerprint}":
        raise ValueError("archived exchange catalog manifest identifier mismatch")
    paths: list[Path] = []
    for relative in (manifest.archive_index_path, manifest.raw_payload_path):
        path = (workspace_root / relative).resolve()
        path.relative_to(workspace_root.resolve())
        if not path.is_file():
            raise ValueError("archived exchange catalog evidence file is missing")
        paths.append(path)
    raw_index, raw_payload = (path.read_bytes() for path in paths)
    if hashlib.sha256(raw_index).hexdigest() != manifest.archive_index_sha256:
        raise ValueError("archived exchange catalog CDX hash mismatch")
    if hashlib.sha256(raw_payload).hexdigest() != manifest.raw_payload_sha256:
        raise ValueError("archived exchange catalog payload hash mismatch")
    timestamp = manifest.archive_capture_at.strftime("%Y%m%d%H%M%S")
    record = (
        _parse_index(raw_index, timestamp=timestamp)
        if manifest.archive_provider == "arquivo_pt"
        else _parse_wayback_index(
            raw_index,
            timestamp=timestamp,
            original_source_url=BINANCE_EXCHANGE_INFO_URL,
        )
    )
    observed_digest = base64.b32encode(hashlib.sha1(raw_payload).digest()).decode("ascii")
    if (
        observed_digest != record["digest"]
        or observed_digest != manifest.archive_digest_sha1_base32
    ):
        raise ValueError("archived exchange catalog CDX digest mismatch")
    server_time, raw_count, entries = _parse_binance_exchange_info(raw_payload)
    if manifest.archive_provider == "internet_archive":
        entries = _with_catalog_source(
            entries,
            "internet_archive_memento_of_binance_exchangeInfo",
        )
    if (
        server_time != manifest.original_server_time
        or raw_count != manifest.raw_symbol_count
        or entries != manifest.entries
        or catalog_archive_sha256(entries) != manifest.catalog_payload_sha256
    ):
        raise ValueError("archived exchange catalog parsed evidence mismatch")
    return {
        "verified": True,
        "manifest_id": manifest.id,
        "archive_provider": manifest.archive_provider,
        "archive_capture_at": manifest.archive_capture_at.isoformat(),
        "original_server_time": manifest.original_server_time.isoformat(),
        "raw_symbol_count": manifest.raw_symbol_count,
        "normalized_record_count": manifest.normalized_record_count,
        "active_record_count": manifest.status_counts.get("TRADING", 0),
        "catalog_payload_sha256": manifest.catalog_payload_sha256,
    }


def verify_archived_catalog_evidence(
    evidence: HistoricalCatalogArchiveEvidence,
    entries: list[MarketCatalogEntry],
    *,
    workspace_root: Path,
) -> dict[str, object]:
    """Rebuild a trusted web-archive claim from its sealed local source files."""

    if evidence.source_kind != "trusted_web_archive_of_official_endpoint":
        raise ValueError("archive evidence is not a trusted web-archive capture")
    required = (
        evidence.archive_capture_at,
        evidence.archive_index_path,
        evidence.raw_payload_path,
        evidence.archive_index_sha256,
        evidence.raw_payload_sha256,
        evidence.archive_digest_sha1_base32,
        evidence.original_server_time,
    )
    if any(value is None for value in required):
        raise ValueError("trusted web-archive evidence is incomplete")
    paths: list[Path] = []
    for relative in (evidence.archive_index_path, evidence.raw_payload_path):
        path = (workspace_root / str(relative)).resolve()
        path.relative_to(workspace_root.resolve())
        if not path.is_file():
            raise ValueError("trusted web-archive evidence file is missing")
        paths.append(path)
    raw_index, raw_payload = (path.read_bytes() for path in paths)
    if hashlib.sha256(raw_index).hexdigest() != evidence.archive_index_sha256:
        raise ValueError("trusted web-archive CDX hash mismatch")
    if hashlib.sha256(raw_payload).hexdigest() != evidence.raw_payload_sha256:
        raise ValueError("trusted web-archive raw payload hash mismatch")
    timestamp = evidence.archive_capture_at.strftime("%Y%m%d%H%M%S")
    if evidence.exchange == "binance" and evidence.archive_provider == "arquivo_pt":
        record = _parse_index(raw_index, timestamp=timestamp)
    elif evidence.exchange == "binance" and evidence.archive_provider == "internet_archive":
        record = _parse_wayback_index(
            raw_index,
            timestamp=timestamp,
            original_source_url=BINANCE_EXCHANGE_INFO_URL,
        )
    elif evidence.exchange == "okx" and evidence.archive_provider == "internet_archive":
        record = _parse_wayback_index(raw_index, timestamp=timestamp)
    else:
        record = None
    if record is None:
        raise ValueError("unsupported trusted web-archive provider")
    digest = base64.b32encode(hashlib.sha1(raw_payload).digest()).decode("ascii")
    if digest != record["digest"] or digest != evidence.archive_digest_sha1_base32:
        raise ValueError("trusted web-archive payload digest mismatch")
    if evidence.exchange == "binance":
        server_time, raw_count, parsed_entries = _parse_binance_exchange_info(raw_payload)
        if evidence.archive_provider == "internet_archive":
            parsed_entries = _with_catalog_source(
                parsed_entries,
                "internet_archive_memento_of_binance_exchangeInfo",
            )
    else:
        raw_count, parsed_entries = _parse_okx_instruments(raw_payload)
        server_time = evidence.archive_capture_at
    if (
        server_time != evidence.original_server_time
        or server_time != evidence.archive_as_of
        or parsed_entries != sorted(entries, key=lambda item: (item.market_id, item.symbol))
        or catalog_archive_sha256(parsed_entries) != evidence.payload_sha256
        or len(parsed_entries) != evidence.record_count
    ):
        raise ValueError("trusted web-archive parsed catalog mismatch")
    return {
        "verified": True,
        "archive_capture_at": evidence.archive_capture_at.isoformat(),
        "original_server_time": server_time.isoformat(),
        "raw_symbol_count": raw_count,
        "record_count": len(parsed_entries),
        "catalog_payload_sha256": evidence.payload_sha256,
    }


def verify_archived_okx_catalog(
    manifest: ArchivedOkxCatalogManifest,
    *,
    workspace_root: Path,
) -> dict[str, object]:
    if manifest.id != f"archived_okx_catalog_{manifest.fingerprint}":
        raise ValueError("archived OKX catalog manifest identifier mismatch")
    verification = verify_archived_catalog_evidence(
        manifest.archive_evidence(),
        manifest.entries,
        workspace_root=workspace_root,
    )
    if (
        verification["raw_symbol_count"] != manifest.raw_symbol_count
        or verification["record_count"] != manifest.normalized_record_count
    ):
        raise ValueError("archived OKX catalog verification counts mismatch")
    return {
        **verification,
        "manifest_id": manifest.id,
        "archive_provider": manifest.archive_provider,
        "active_record_count": manifest.status_counts.get("live", 0),
    }


def save_archived_catalog_manifest(
    manifest: ArchivedExchangeCatalogManifest | ArchivedOkxCatalogManifest,
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.partial")
    temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination
