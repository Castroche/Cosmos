from __future__ import annotations

import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, computed_field

BINANCE_VISION_INDEX_URL = (
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    "?delimiter=/&prefix=data/futures/um/monthly/klines/"
)
BYBIT_PUBLIC_TRADING_INDEX_URL = "https://public.bybit.com/trading/"

_OFFICIAL_HOSTS = {
    "binance": {"s3-ap-northeast-1.amazonaws.com"},
    "bybit": {"public.bybit.com"},
}
_USDT_PERPETUAL = re.compile(r"^([A-Z0-9]+)USDT$")


class OfficialArchiveInstrument(BaseModel):
    market_id: str
    symbol: str
    quote_currency: str = "USDT"


class OfficialArchiveIndex(BaseModel):
    version: int = 1
    id: str
    exchange: Literal["binance", "bybit"]
    market_type: Literal["swap"] = "swap"
    source_url: str
    source_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_listing_complete: bool
    instruments: list[OfficialArchiveInstrument]
    raw_market_count: int = Field(ge=1)
    normalized_usdt_perpetual_count: int = Field(ge=1)
    historical_point_in_time_complete: bool = False
    limitations: list[str] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def fingerprint(self) -> str:
        # Retrieval time describes the observation, not the exchange-owned payload.
        # Excluding it makes repeated captures of identical official evidence idempotent.
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


def _validated_official_url(exchange: str, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS.get(exchange, set()):
        raise ValueError(f"{exchange} archive index must use an allowlisted official HTTPS host")
    if exchange == "binance" and parsed.path != "/data.binance.vision":
        raise ValueError("Binance archive index path is not the official data bucket")
    if exchange == "bybit" and parsed.path.rstrip("/") != "/trading":
        raise ValueError("Bybit archive index path is not the official trading archive root")


def _opener(proxy_url: str):
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    return urllib.request.build_opener(*handlers) if proxy_url else urllib.request.build_opener()


def download_official_archive_index(
    exchange: Literal["binance", "bybit"],
    *,
    proxy_url: str = "",
    timeout_seconds: int = 60,
    maximum_bytes: int = 5_000_000,
    fetcher: Callable[[str], bytes] | None = None,
) -> OfficialArchiveIndex:
    """Fetch an exchange-owned historical symbol index without trusting client payloads.

    This proves historical *presence* only. It intentionally cannot certify a point-in-time
    investable universe until listing state and contemporaneous liquidity are derived for a
    frozen historical anchor.
    """

    source_url = (
        BINANCE_VISION_INDEX_URL if exchange == "binance" else BYBIT_PUBLIC_TRADING_INDEX_URL
    )
    _validated_official_url(exchange, source_url)
    if fetcher is None:
        opener = _opener(proxy_url)

        def fetcher(url: str) -> bytes:
            with opener.open(url, timeout=timeout_seconds) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > maximum_bytes:
                    raise ValueError("official archive index exceeds the maximum response size")
                payload = response.read(maximum_bytes + 1)
                if len(payload) > maximum_bytes:
                    raise ValueError("official archive index exceeds the maximum response size")
                return payload

    raw = fetcher(source_url)
    if not raw:
        raise ValueError("official archive index returned an empty payload")
    if len(raw) > maximum_bytes:
        raise ValueError("official archive index exceeds the maximum response size")
    if exchange == "binance":
        market_ids, complete = _parse_binance_vision_index(raw)
    else:
        market_ids, complete = _parse_bybit_public_index(raw)
    instruments = _normalize_usdt_perpetuals(market_ids)
    if not complete:
        raise ValueError("official archive index is paginated or incomplete")
    if not instruments:
        raise ValueError("official archive index contains no normalized USDT perpetual markets")
    retrieved_at = datetime.now(UTC)
    provisional = OfficialArchiveIndex(
        id="pending",
        exchange=exchange,
        source_url=source_url,
        source_payload_sha256=hashlib.sha256(raw).hexdigest(),
        source_listing_complete=complete,
        instruments=instruments,
        raw_market_count=len(market_ids),
        normalized_usdt_perpetual_count=len(instruments),
        historical_point_in_time_complete=False,
        limitations=[
            "The official directory proves that a market has archived observations, not that "
            "it was tradable or liquid at an arbitrary historical timestamp.",
            "Point-in-time certification additionally requires contemporaneous listing state, "
            "quote volume and complete candidate coverage at the frozen anchor.",
        ],
        retrieved_at=retrieved_at,
    )
    return provisional.model_copy(update={"id": f"official_archive_{provisional.fingerprint}"})


def _parse_binance_vision_index(raw: bytes) -> tuple[list[str], bool]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Binance archive index is not valid XML") from exc
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    bucket = root.findtext("s3:Name", namespaces=namespace)
    prefix = root.findtext("s3:Prefix", namespaces=namespace)
    truncated = root.findtext("s3:IsTruncated", namespaces=namespace)
    if bucket != "data.binance.vision" or prefix != "data/futures/um/monthly/klines/":
        raise ValueError("Binance archive index bucket or prefix does not match the official scope")
    market_ids: list[str] = []
    for node in root.findall("s3:CommonPrefixes", namespace):
        value = node.findtext("s3:Prefix", namespaces=namespace) or ""
        parts = value.rstrip("/").split("/")
        if len(parts) == 6:
            market_ids.append(parts[-1].upper())
    return sorted(set(market_ids)), truncated == "false"


def _parse_bybit_public_index(raw: bytes) -> tuple[list[str], bool]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Bybit archive index is not UTF-8 HTML") from exc
    if "Directory listing for /trading/" not in text:
        raise ValueError("Bybit archive index does not identify the official trading directory")
    hrefs = re.findall(r'href=["\'](?:/trading/)?([^/"\']+)/["\']', text, flags=re.I)
    market_ids = sorted({value.upper() for value in hrefs if value not in {".", ".."}})
    # The official directory is a single unpaginated HTML listing.
    return market_ids, bool(market_ids)


def _normalize_usdt_perpetuals(market_ids: list[str]) -> list[OfficialArchiveInstrument]:
    instruments: list[OfficialArchiveInstrument] = []
    for market_id in sorted(set(market_ids)):
        match = _USDT_PERPETUAL.fullmatch(market_id)
        if match is None:
            continue
        base = match.group(1)
        instruments.append(
            OfficialArchiveInstrument(
                market_id=market_id,
                symbol=f"{base}/USDT",
            )
        )
    return instruments
