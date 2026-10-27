from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import uuid4

from ..domain import (
    BasisPoint,
    Candle,
    Fill,
    FundingRatePoint,
    MarketSnapshot,
    OpenInterestPoint,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioState,
    Position,
    PositioningPoint,
    PositionSide,
    Side,
    StrategyPlan,
    TradingMode,
)
from ..research_universe import MarketCatalogEntry
from ..storage import SqliteStore
from ..timeframes import TIMEFRAME_SECONDS
from .base import ExchangeAdapter, OrderSubmissionUncertain, PositionProtectionError


class ExchangeConfigurationError(RuntimeError):
    """A private-account configuration failure with an actionable machine code."""

    def __init__(self, code: str, message: str, required_action: str):
        super().__init__(message)
        self.code = code
        self.required_action = required_action


@dataclass(frozen=True)
class _OkxProtection:
    algo_id: str
    client_id: str
    size: Decimal
    state: str
    exit_order_ids: tuple[str, ...] = ()
    side: str = ""
    position_side: str = ""
    instrument_id: str = ""
    stop_price: str = ""
    target_price: str = ""


class CcxtExchangeAdapter(ExchangeAdapter):
    """Optional CCXT adapter. Install with `pip install -e .[exchange]`.

    Production order placement is guarded by both `sandbox` and `live_enabled`. This
    class intentionally exposes a narrow subset of CCXT instead of leaking vendor
    response shapes into the domain.
    """

    margin_mode = "cross"
    contract_leverage = Decimal("3")
    public_fetch_attempts = 4

    @staticmethod
    def _ccxt_client_exchange_id(exchange_id: str, market_type: str) -> str:
        """Select a venue client whose market discovery matches the requested scope.

        CCXT's generic Binance client loads spot and derivative catalogs together.
        A blocked spot host can therefore prevent USD-M research even when every
        futures endpoint is healthy. The dedicated USD-M client is scope-exact and
        avoids that unrelated dependency while the public COSMOS venue identity
        remains ``binance``.
        """

        if exchange_id == "binance" and market_type == "swap":
            return "binanceusdm"
        return exchange_id

    def __init__(
        self,
        exchange_id: str,
        credentials: dict[str, str],
        *,
        sandbox: bool = True,
        live_enabled: bool = False,
        public_only: bool = False,
        market_type: str = "spot",
        margin_mode: str = "cross",
        contract_leverage: Decimal = Decimal("3"),
        store: SqliteStore | None = None,
        account_id: str | None = None,
        proxy_url: str = "",
        entry_submission_guard=None,
    ):
        self.entry_submission_guard = entry_submission_guard
        try:
            import ccxt.async_support as ccxt
        except ImportError as exc:
            raise RuntimeError("install the 'exchange' extra to use CCXT") from exc
        client_exchange_id = self._ccxt_client_exchange_id(exchange_id, market_type)
        exchange_class = getattr(ccxt, client_exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"unsupported CCXT exchange: {client_exchange_id}")
        self.exchange_id = exchange_id
        self.sandbox = sandbox
        self.live_enabled = live_enabled
        self.public_only = public_only
        self.market_type = market_type
        self.margin_mode = margin_mode
        self.contract_leverage = Decimal(str(contract_leverage))
        self.store = store
        self.account_id = account_id or f"{exchange_id}:{market_type}"
        client_options: dict[str, Any] = {"defaultType": market_type}
        # Writes must never inherit CCXT transport retries: an ACK can be lost.
        client_options["maxRetriesOnFailure"] = 0
        if exchange_id == "okx" and market_type == "swap":
            # Scope discovery to the venue actually used by COSMOS. Loading OKX
            # FUTURES/OPTION catalogs can otherwise make Demo swap reconciliation
            # fail because of an unrelated public endpoint timeout.
            client_options["fetchMarkets"] = {"types": ["swap"]}
        client_configuration = {
            "apiKey": credentials.get("api_key", ""),
            "secret": credentials.get("secret", ""),
            "password": credentials.get("passphrase", ""),
            "enableRateLimit": True,
            "options": client_options,
        }
        if proxy_url:
            client_configuration["httpsProxy"] = proxy_url
        self.client: Any = exchange_class(client_configuration)
        if sandbox:
            # CCXT requires sandbox mode to be the first call after construction.
            self.client.set_sandbox_mode(True)

    async def health(self) -> dict[str, str | bool]:
        await self.client.load_markets()
        return {
            "ok": True,
            "mode": "public" if self.public_only else ("sandbox" if self.sandbox else "live"),
            "venue": self.exchange_id,
        }

    @staticmethod
    def _catalog_timestamp(value) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if numeric > 10_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _catalog_decimal(value) -> Decimal | None:
        if value is None or value == "":
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() and parsed >= 0 else None

    @staticmethod
    def _catalog_asset_class(info: dict) -> tuple[str, str | None, str | None, list[str]]:
        classification_keys = (
            "underlyingType",
            "underlyingSubType",
            "assetClass",
            "assetType",
            "category",
            "productType",
        )
        source_keys: list[str] = []
        values: list[str] = []
        underlying_type = None
        underlying_subtypes: list[str] = []
        for key in classification_keys:
            raw = info.get(key)
            if raw is None or raw == "":
                continue
            source_keys.append(key)
            raw_values = raw if isinstance(raw, list) else [raw]
            normalized = [str(item).strip() for item in raw_values if str(item).strip()]
            values.extend(normalized)
            if key == "underlyingType" and normalized:
                underlying_type = normalized[0]
            elif key == "underlyingSubType":
                underlying_subtypes.extend(normalized)
        evidence = " ".join(values).upper().replace("-", "_")
        non_crypto_markers = (
            "STOCK",
            "EQUITY",
            "FOREX",
            "FX",
            "COMMODITY",
            "METAL",
            "PRECIOUS",
            "BOND",
            "TREASURY",
        )
        crypto_markers = (
            "COIN",
            "CRYPTO",
            "DIGITAL_ASSET",
            "BLOCKCHAIN",
            "DEFI",
            "LAYER_1",
            "LAYER_2",
        )
        if any(marker in evidence for marker in non_crypto_markers):
            asset_class = "non_crypto"
        elif any(marker in evidence for marker in crypto_markers):
            asset_class = "crypto"
        else:
            asset_class = "unknown"
        source = ",".join(f"market.info.{key}" for key in source_keys) if source_keys else None
        return asset_class, source, underlying_type, underlying_subtypes

    async def research_market_catalog(
        self,
        *,
        include_tickers: bool = True,
    ) -> list[MarketCatalogEntry]:
        """Return an auditable point-in-time public market catalog.

        Inactive instruments returned by the venue remain in the artifact. The
        catalog does not claim to reconstruct markets omitted after delisting.
        """

        await self._public_request(self.client.load_markets)
        tickers: dict[str, dict] = {}
        if include_tickers and getattr(self.client, "has", {}).get("fetchTickers"):
            try:
                tickers = await self._public_request(self.client.fetch_tickers)
            except Exception:
                tickers = {}
        results: list[MarketCatalogEntry] = []
        listing_keys = (
            "onboardDate",
            "launchTime",
            "listTime",
            "listingTime",
            "createdTime",
        )
        for client_symbol, market in sorted(self.client.markets.items()):
            info = market.get("info") or {}
            listed_at = None
            listing_source = None
            for key in listing_keys:
                listed_at = self._catalog_timestamp(info.get(key))
                if listed_at is not None:
                    listing_source = f"market.info.{key}"
                    break
            ticker = tickers.get(client_symbol) or {}
            raw_quote_volume = ticker.get("quoteVolume")
            if raw_quote_volume is None or raw_quote_volume == "":
                base_volume = ticker.get("baseVolume")
                last = ticker.get("last")
                parsed_base_volume = self._catalog_decimal(base_volume)
                parsed_last = self._catalog_decimal(last)
                if parsed_base_volume is not None and parsed_last is not None:
                    raw_quote_volume = parsed_base_volume * parsed_last
            quote_volume = self._catalog_decimal(raw_quote_volume)
            (
                asset_class,
                asset_class_source,
                underlying_type,
                underlying_subtypes,
            ) = self._catalog_asset_class(info)
            market_type = str(market.get("type") or "")
            if not market_type:
                market_type = next(
                    (
                        candidate
                        for candidate in ("spot", "swap", "future", "option")
                        if market.get(candidate)
                    ),
                    "unknown",
                )
            results.append(
                MarketCatalogEntry(
                    exchange=self.exchange_id,
                    market_id=str(market.get("id") or client_symbol),
                    symbol=self._canonical_symbol(str(market.get("symbol") or client_symbol)),
                    base=str(market.get("base") or "").upper(),
                    quote=str(market.get("quote") or "").upper(),
                    settle=(
                        str(market.get("settle")).upper()
                        if market.get("settle") not in {None, ""}
                        else None
                    ),
                    market_type=market_type,
                    active=market.get("active"),
                    contract=bool(market.get("contract")),
                    linear=market.get("linear"),
                    inverse=market.get("inverse"),
                    listed_at=listed_at,
                    listing_time_source=listing_source,
                    expiry=self._catalog_timestamp(market.get("expiry")),
                    status=str(info.get("status") or info.get("state") or "") or None,
                    quote_volume_24h=quote_volume,
                    asset_class=asset_class,
                    asset_class_source=asset_class_source,
                    underlying_type=underlying_type,
                    underlying_subtypes=underlying_subtypes,
                )
            )
        return results

    async def _public_request(self, operation, *args, **kwargs):
        """Retry idempotent public-data reads with bounded exponential backoff."""

        last_error: Exception | None = None
        for attempt in range(self.public_fetch_attempts):
            try:
                return await operation(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= self.public_fetch_attempts:
                    break
                await asyncio.sleep(
                    float(
                        min(
                            Decimal("0.25") * (2**attempt),
                            Decimal("2"),
                        )
                    )
                )
        assert last_error is not None
        raise last_error

    async def market_snapshot(self, symbol: str) -> MarketSnapshot:
        client_symbol = await self._resolve_symbol(symbol)
        ticker = await self._public_request(
            self.client.fetch_ticker,
            client_symbol,
        )
        return MarketSnapshot(
            symbol=self._canonical_symbol(client_symbol),
            timestamp=datetime.fromtimestamp(ticker["timestamp"] / 1000, tz=UTC),
            bid=Decimal(str(ticker["bid"])),
            ask=Decimal(str(ticker["ask"])),
            last=Decimal(str(ticker["last"])),
            source=self.exchange_id,
        )

    async def funding_snapshot(self, symbol: str) -> dict | None:
        if self.market_type != "swap" or not self.client.has.get("fetchFundingRate"):
            return None
        client_symbol = await self._resolve_symbol(symbol)
        now = datetime.now(UTC)
        cache = getattr(self, "_funding_cache", {})
        cached = cache.get(client_symbol)
        if cached and (now - cached[0]).total_seconds() < 300:
            return cached[1]
        payload = await self.client.fetch_funding_rate(client_symbol)
        raw_rate = payload.get("fundingRate")
        if raw_rate in (None, ""):
            return None
        funding_timestamp = payload.get("fundingTimestamp")
        result = {
            "funding_rate": Decimal(str(raw_rate)),
            "funding_time": (
                datetime.fromtimestamp(funding_timestamp / 1000, tz=UTC)
                if funding_timestamp
                else None
            ),
            "funding_interval": payload.get("interval"),
        }
        raw_mark = payload.get("markPrice")
        raw_index = payload.get("indexPrice")
        if raw_mark not in (None, "") and raw_index not in (None, ""):
            mark_price = Decimal(str(raw_mark))
            index_price = Decimal(str(raw_index))
            if mark_price > 0 and index_price > 0:
                result.update(
                    {
                        "mark_price": mark_price,
                        "index_price": index_price,
                        "basis_rate": (mark_price - index_price) / index_price,
                        "basis_time": now,
                    }
                )
        cache[client_symbol] = (now, result)
        self._funding_cache = cache
        return result

    async def market_microstructure(self, symbol: str) -> dict | None:
        """Fetch bounded public microstructure evidence with short-lived coalescing."""

        client_symbol = await self._resolve_symbol(symbol)
        now = datetime.now(UTC)
        cache = getattr(self, "_microstructure_cache", {})
        cached = cache.get(client_symbol)
        if cached and (now - cached[0]).total_seconds() < 30:
            return cached[1]
        inflight = getattr(self, "_microstructure_inflight", {})
        task = inflight.get(client_symbol)
        if task is None:
            task = asyncio.create_task(
                self._fetch_market_microstructure(client_symbol),
                name=f"microstructure:{client_symbol}",
            )
            inflight[client_symbol] = task
            self._microstructure_inflight = inflight
        try:
            result = await task
        finally:
            inflight.pop(client_symbol, None)
        cache[client_symbol] = (now, result)
        self._microstructure_cache = cache
        return result

    async def _fetch_market_microstructure(self, client_symbol: str) -> dict:
        capabilities = getattr(self.client, "has", {})
        market = self.client.markets.get(client_symbol) or {}
        requests: list[tuple[str, Any]] = []
        if capabilities.get("fetchOrderBook"):
            requests.append(("order_book", self.client.fetch_order_book(client_symbol, 20)))
        if capabilities.get("fetchTrades"):
            requests.append(("trades", self.client.fetch_trades(client_symbol, limit=100)))
        if self.market_type == "swap" and capabilities.get("fetchOpenInterest"):
            requests.append(("open_interest", self.client.fetch_open_interest(client_symbol)))
        if self.market_type == "swap" and capabilities.get("fetchOpenInterestHistory"):
            history_symbol = (
                market.get("base") if getattr(self, "exchange_id", "") == "okx" else client_symbol
            )
            if history_symbol:
                requests.append(
                    (
                        "open_interest_history",
                        self.client.fetch_open_interest_history(
                            history_symbol,
                            timeframe="1h",
                            since=int((datetime.now(UTC) - timedelta(hours=26)).timestamp() * 1000),
                            limit=30,
                        ),
                    )
                )
        if self.market_type == "swap" and capabilities.get("fetchLongShortRatioHistory"):
            requests.append(
                (
                    "positioning",
                    self.client.fetch_long_short_ratio_history(
                        client_symbol,
                        timeframe="1h",
                        since=int((datetime.now(UTC) - timedelta(hours=3)).timestamp() * 1000),
                        limit=4,
                    ),
                )
            )
        if not requests:
            return {
                "status": "unavailable",
                "observed_at": datetime.now(UTC),
            }
        results = await asyncio.gather(
            *(request for _, request in requests),
            return_exceptions=True,
        )
        payloads = {
            name: result
            for (name, _), result in zip(requests, results, strict=True)
            if not isinstance(result, Exception)
        }
        contract_size = Decimal(str(market.get("contractSize") or 1))
        output: dict[str, Any] = {
            "status": "partial",
            "observed_at": datetime.now(UTC),
        }
        order_book = payloads.get("order_book")
        if isinstance(order_book, dict):
            bids = order_book.get("bids") or []
            asks = order_book.get("asks") or []
            bid_depth = sum(
                (
                    Decimal(str(level[0])) * Decimal(str(level[1])) * contract_size
                    for level in bids[:20]
                    if len(level) >= 2
                ),
                Decimal("0"),
            )
            ask_depth = sum(
                (
                    Decimal(str(level[0])) * Decimal(str(level[1])) * contract_size
                    for level in asks[:20]
                    if len(level) >= 2
                ),
                Decimal("0"),
            )
            total_depth = bid_depth + ask_depth
            output.update(
                {
                    "bid_depth_quote": bid_depth,
                    "ask_depth_quote": ask_depth,
                    "order_book_imbalance": (
                        (bid_depth - ask_depth) / total_depth if total_depth > 0 else Decimal("0")
                    ),
                }
            )
        trades = payloads.get("trades")
        if isinstance(trades, list):
            buy_flow = Decimal("0")
            sell_flow = Decimal("0")
            timestamps: list[int] = []
            for trade in trades:
                price = Decimal(str(trade.get("price") or 0))
                amount = Decimal(str(trade.get("amount") or 0))
                notional = price * amount * contract_size
                if trade.get("side") == "buy":
                    buy_flow += notional
                elif trade.get("side") == "sell":
                    sell_flow += notional
                if trade.get("timestamp") is not None:
                    timestamps.append(int(trade["timestamp"]))
            total_flow = buy_flow + sell_flow
            output["trade_flow_imbalance"] = (
                (buy_flow - sell_flow) / total_flow if total_flow > 0 else Decimal("0")
            )
            output["trade_flow_quote"] = total_flow
            output["active_buy_quote"] = buy_flow
            output["active_sell_quote"] = sell_flow
            if len(timestamps) >= 2:
                elapsed_seconds = max((max(timestamps) - min(timestamps)) / 1000, 1)
                output["trade_intensity"] = Decimal(len(timestamps)) / Decimal(str(elapsed_seconds))
        bid_depth = output.get("bid_depth_quote")
        ask_depth = output.get("ask_depth_quote")
        if bid_depth is not None and ask_depth is not None:
            liquidity = Decimal(str(bid_depth)) + Decimal(str(ask_depth))
            output["liquidity_quote"] = liquidity
            output["estimated_slippage_bps"] = (
                min(Decimal("10000") / liquidity, Decimal("0.01")) * Decimal("10000")
                if liquidity > 0
                else Decimal("10000")
            )
        open_interest = payloads.get("open_interest")
        current_open_interest: Decimal | None = None
        if isinstance(open_interest, dict):
            amount = open_interest.get("openInterestAmount")
            value = open_interest.get("openInterestValue")
            if amount not in (None, ""):
                current_open_interest = Decimal(str(amount))
                output["open_interest"] = current_open_interest
            if value not in (None, ""):
                output["open_interest_value"] = Decimal(str(value))
        open_interest_history = payloads.get("open_interest_history")
        if isinstance(open_interest_history, list):
            normalized_history: list[tuple[int, Decimal]] = []
            history_metric = ""
            for item in open_interest_history:
                timestamp = item.get("timestamp")
                amount = item.get("openInterestAmount")
                value = item.get("openInterestValue")
                raw_history_value = amount if amount not in (None, "") else value
                metric = (
                    "amount"
                    if amount not in (None, "")
                    else "value"
                    if value not in (None, "")
                    else ""
                )
                if (
                    timestamp is None
                    or raw_history_value in (None, "")
                    or (history_metric and metric != history_metric)
                ):
                    continue
                history_metric = metric
                normalized_history.append(
                    (
                        int(timestamp),
                        Decimal(str(raw_history_value)),
                    )
                )
            normalized_history.sort(key=lambda item: item[0])
            if normalized_history:
                output["open_interest_history_points"] = len(normalized_history)
                output["open_interest_history_metric"] = history_metric
                latest_timestamp, latest_history_value = normalized_history[-1]
                for hours in (4, 24):
                    cutoff = latest_timestamp - hours * 60 * 60 * 1000
                    baseline = next(
                        (
                            amount
                            for timestamp, amount in reversed(normalized_history)
                            if timestamp <= cutoff
                        ),
                        None,
                    )
                    if baseline is None or baseline == 0:
                        continue
                    output[f"open_interest_change_{hours}h"] = (
                        latest_history_value - baseline
                    ) / abs(baseline)
        positioning = payloads.get("positioning")
        if isinstance(positioning, list) and positioning:
            latest_positioning = max(
                (
                    item
                    for item in positioning
                    if item.get("timestamp") is not None
                    and item.get("longShortRatio") not in (None, "")
                ),
                key=lambda item: int(item["timestamp"]),
                default=None,
            )
            if latest_positioning is not None:
                info = latest_positioning.get("info") or {}
                output["long_short_ratio"] = Decimal(str(latest_positioning["longShortRatio"]))
                if info.get("longAccount") not in (None, ""):
                    output["long_account_fraction"] = Decimal(str(info["longAccount"]))
                if info.get("shortAccount") not in (None, ""):
                    output["short_account_fraction"] = Decimal(str(info["shortAccount"]))
                output["positioning_time"] = datetime.fromtimestamp(
                    int(latest_positioning["timestamp"]) / 1000,
                    tz=UTC,
                )
        evidence_count = sum(
            key in output
            for key in (
                "order_book_imbalance",
                "trade_flow_imbalance",
                "open_interest",
            )
        )
        output["status"] = (
            "complete" if evidence_count == 3 else "partial" if evidence_count else "unavailable"
        )
        return output

    async def fetch_funding_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[FundingRatePoint]:
        capabilities = getattr(self.client, "has", {})
        if self.market_type != "swap" or not capabilities.get("fetchFundingRateHistory"):
            return []
        client_symbol = await self._resolve_symbol(symbol)
        now = datetime.now(UTC)
        cache_key = (client_symbol, start.isoformat(), end.isoformat())
        history_cache = getattr(self, "_funding_history_cache", {})
        cached = history_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 300:
            return cached[1]
        inflight = getattr(self, "_funding_history_inflight", {})
        task = inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_funding_history_points(
                    client_symbol,
                    start,
                    end,
                ),
                name=f"funding-history:{client_symbol}",
            )
            inflight[cache_key] = task
            self._funding_history_inflight = inflight
        try:
            result = await asyncio.shield(task)
        finally:
            if inflight.get(cache_key) is task and task.done():
                inflight.pop(cache_key, None)
        history_cache[cache_key] = (now, result)
        self._funding_history_cache = history_cache
        return result

    async def _fetch_funding_history_points(
        self,
        client_symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[FundingRatePoint]:
        since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        points: dict[int, FundingRatePoint] = {}
        while since <= end_ms:
            batch = await self._public_request(
                self.client.fetch_funding_rate_history,
                client_symbol,
                since=since,
                limit=100,
            )
            if not batch:
                break
            last_timestamp = since
            for item in batch:
                timestamp = item.get("timestamp") or item.get("fundingTimestamp")
                raw_rate = item.get("fundingRate")
                if timestamp is None:
                    continue
                timestamp = int(timestamp)
                last_timestamp = max(last_timestamp, timestamp)
                if timestamp < int(start.timestamp() * 1000) or timestamp > end_ms:
                    continue
                if raw_rate in (None, ""):
                    continue
                points[timestamp] = FundingRatePoint(
                    timestamp=datetime.fromtimestamp(
                        timestamp / 1000,
                        tz=UTC,
                    ),
                    rate=Decimal(str(raw_rate)),
                    mark_price=(
                        Decimal(str(item["markPrice"]))
                        if item.get("markPrice") not in (None, "")
                        else None
                    ),
                )
            next_since = last_timestamp + 1
            if next_since <= since or last_timestamp >= end_ms or len(batch) < 100:
                break
            since = next_since
        return [points[key] for key in sorted(points)]

    async def fetch_open_interest_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[OpenInterestPoint]:
        capabilities = getattr(self.client, "has", {})
        if self.market_type != "swap" or not capabilities.get("fetchOpenInterestHistory"):
            return []
        # OKX rejects requests whose Rubik statistics window starts outside
        # its bounded public retention range with code 50030. Clamp only the
        # request; the immutable dataset keeps the original market-data bounds
        # so downstream coverage remains explicit rather than backfilled.
        if getattr(self, "exchange_id", "") == "okx":
            start = max(start, end - timedelta(days=30))
        elif getattr(self, "exchange_id", "") == "binance":
            # Binance USDⓈ-M open-interest statistics reject a startTime on
            # the exact 30-day retention boundary. Keep one day of margin so
            # clock skew and request latency cannot turn a valid public query
            # into -1130 "startTime is invalid".
            start = max(start, end - timedelta(days=29))
        client_symbol = await self._resolve_symbol(symbol)
        market = getattr(self.client, "markets", {}).get(client_symbol) or {}
        history_symbol = (
            market.get("base") if getattr(self, "exchange_id", "") == "okx" else client_symbol
        )
        if not history_symbol:
            return []
        history_timeframe = timeframe if timeframe in {"5m", "1h", "1d"} else "1h"
        now = datetime.now(UTC)
        cache_key = (
            client_symbol,
            history_timeframe,
            start.isoformat(),
            end.isoformat(),
        )
        history_cache = getattr(self, "_open_interest_history_cache", {})
        cached = history_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 300:
            return cached[1]
        inflight = getattr(self, "_open_interest_history_inflight", {})
        task = inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_open_interest_history_points(
                    history_symbol,
                    history_timeframe,
                    start,
                    end,
                ),
                name=f"open-interest-history:{client_symbol}:{history_timeframe}",
            )
            inflight[cache_key] = task
            self._open_interest_history_inflight = inflight
        try:
            result = await asyncio.shield(task)
        finally:
            if inflight.get(cache_key) is task and task.done():
                inflight.pop(cache_key, None)
        history_cache[cache_key] = (now, result)
        self._open_interest_history_cache = history_cache
        return result

    async def _fetch_open_interest_history_points(
        self,
        history_symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[OpenInterestPoint]:
        start_ms = int(start.timestamp() * 1000)
        since = start_ms
        end_ms = int(end.timestamp() * 1000)
        points: dict[int, OpenInterestPoint] = {}
        while since <= end_ms:
            batch = await self._public_request(
                self.client.fetch_open_interest_history,
                history_symbol,
                timeframe=timeframe,
                since=since,
                limit=100,
            )
            if not batch:
                break
            last_timestamp = since
            for item in batch:
                timestamp = item.get("timestamp")
                if timestamp is None:
                    continue
                timestamp = int(timestamp)
                last_timestamp = max(last_timestamp, timestamp)
                if timestamp < start_ms or timestamp > end_ms:
                    continue
                raw_amount = item.get("openInterestAmount")
                raw_value = item.get("openInterestValue")
                amount = Decimal(str(raw_amount)) if raw_amount not in (None, "") else None
                value = Decimal(str(raw_value)) if raw_value not in (None, "") else None
                if amount is None and value is None:
                    continue
                points[timestamp] = OpenInterestPoint(
                    timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                    open_interest_amount=amount,
                    open_interest_value=value,
                )
            next_since = last_timestamp + 1
            if next_since <= since or last_timestamp >= end_ms or len(batch) < 100:
                break
            since = next_since
        return [points[key] for key in sorted(points)]

    async def fetch_positioning_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[PositioningPoint]:
        capabilities = getattr(self.client, "has", {})
        if self.market_type != "swap" or not capabilities.get("fetchLongShortRatioHistory"):
            return []
        if getattr(self, "exchange_id", "") == "binance":
            # Binance only exposes roughly 30 days through this endpoint. The
            # one-day margin avoids boundary rejection caused by clock skew.
            start = max(start, end - timedelta(days=29))
        client_symbol = await self._resolve_symbol(symbol)
        history_timeframe = (
            timeframe
            if timeframe in {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
            else "1h"
        )
        now = datetime.now(UTC)
        cache_key = (
            client_symbol,
            history_timeframe,
            start.isoformat(),
            end.isoformat(),
        )
        history_cache = getattr(self, "_positioning_history_cache", {})
        cached = history_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 300:
            return cached[1]
        inflight = getattr(self, "_positioning_history_inflight", {})
        task = inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_positioning_history_points(
                    client_symbol,
                    history_timeframe,
                    start,
                    end,
                ),
                name=f"positioning-history:{client_symbol}:{history_timeframe}",
            )
            inflight[cache_key] = task
            self._positioning_history_inflight = inflight
        try:
            result = await asyncio.shield(task)
        finally:
            if inflight.get(cache_key) is task and task.done():
                inflight.pop(cache_key, None)
        history_cache[cache_key] = (now, result)
        self._positioning_history_cache = history_cache
        return result

    async def _fetch_positioning_history_points(
        self,
        history_symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[PositioningPoint]:
        period_ms = TIMEFRAME_SECONDS[timeframe] * 1000
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        cursor = start_ms
        points: dict[int, PositioningPoint] = {}
        while cursor <= end_ms:
            # Binance returns the latest rows in a wide requested window. Bind
            # every request to a <=500-observation causal window with `until`.
            chunk_end = min(end_ms, cursor + (499 * period_ms))
            batch = await self._public_request(
                self.client.fetch_long_short_ratio_history,
                history_symbol,
                timeframe=timeframe,
                since=cursor,
                limit=500,
                params={"until": chunk_end},
            )
            for item in batch or []:
                raw_timestamp = item.get("timestamp")
                raw_ratio = item.get("longShortRatio")
                if raw_timestamp is None or raw_ratio in (None, ""):
                    continue
                timestamp = int(raw_timestamp)
                if timestamp < start_ms or timestamp > end_ms:
                    continue
                info = item.get("info") or {}
                raw_long = info.get("longAccount")
                raw_short = info.get("shortAccount")
                points[timestamp] = PositioningPoint(
                    timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                    long_short_ratio=Decimal(str(raw_ratio)),
                    long_account_fraction=(
                        Decimal(str(raw_long)) if raw_long not in (None, "") else None
                    ),
                    short_account_fraction=(
                        Decimal(str(raw_short)) if raw_short not in (None, "") else None
                    ),
                    source_variant="global_account",
                )
            next_cursor = chunk_end + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return [points[key] for key in sorted(points)]

    async def fetch_basis_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[BasisPoint]:
        capabilities = getattr(self.client, "has", {})
        if self.market_type != "swap" or not (
            capabilities.get("fetchMarkOHLCV") and capabilities.get("fetchIndexOHLCV")
        ):
            return []
        client_symbol = await self._resolve_symbol(symbol)
        history_timeframe = timeframe if timeframe in TIMEFRAME_SECONDS else "1h"
        now = datetime.now(UTC)
        cache_key = (
            client_symbol,
            history_timeframe,
            start.isoformat(),
            end.isoformat(),
        )
        history_cache = getattr(self, "_basis_history_cache", {})
        cached = history_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 300:
            return cached[1]
        # Keep the two long pagination streams sequential. Running them in
        # parallel doubles burst weight and can exhaust a shared Binance IP
        # budget even though each individual CCXT request is rate-limited.
        mark_rows = await self._fetch_derivative_price_ohlcv(
            self.client.fetch_mark_ohlcv,
            client_symbol,
            history_timeframe,
            start,
            end,
        )
        index_rows = await self._fetch_derivative_price_ohlcv(
            self.client.fetch_index_ohlcv,
            client_symbol,
            history_timeframe,
            start,
            end,
        )
        mark_prices = {timestamp: close for timestamp, close in mark_rows}
        index_prices = {timestamp: close for timestamp, close in index_rows}
        points = []
        for timestamp in sorted(mark_prices.keys() & index_prices.keys()):
            mark_price = mark_prices[timestamp]
            index_price = index_prices[timestamp]
            if mark_price <= 0 or index_price <= 0:
                continue
            points.append(
                BasisPoint(
                    timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                    mark_price=mark_price,
                    index_price=index_price,
                    basis_rate=(mark_price - index_price) / index_price,
                )
            )
        history_cache[cache_key] = (now, points)
        self._basis_history_cache = history_cache
        return points

    async def _fetch_derivative_price_ohlcv(
        self,
        operation,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[int, Decimal]]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        cursor = start_ms
        rows: dict[int, Decimal] = {}
        # CCXT clamps OKX mark/index candle calls to 100 rows even when a
        # larger limit is requested. Compare against the effective venue
        # limit so one full 100-row page does not look like a terminal short
        # page and silently truncate years of basis history.
        batch_limit = 100 if getattr(self, "exchange_id", "") == "okx" else 1000
        while cursor <= end_ms:
            batch = await self._public_request(
                operation,
                symbol,
                timeframe=timeframe,
                since=cursor,
                limit=batch_limit,
            )
            if not batch:
                break
            last_timestamp = cursor
            for row in batch:
                if len(row) < 5:
                    continue
                timestamp = int(row[0])
                last_timestamp = max(last_timestamp, timestamp)
                if start_ms <= timestamp <= end_ms:
                    rows[timestamp] = Decimal(str(row[4]))
            next_cursor = last_timestamp + 1
            if next_cursor <= cursor or last_timestamp >= end_ms or len(batch) < batch_limit:
                break
            cursor = next_cursor
        return [(timestamp, rows[timestamp]) for timestamp in sorted(rows)]

    async def fetch_candles(
        self, symbol: str, timeframe: str = "1h", limit: int = 500
    ) -> list[Candle]:
        client_symbol = await self._resolve_symbol(symbol)
        timeframe_ms = TIMEFRAME_SECONDS[timeframe] * 1000
        since = int(datetime.now(UTC).timestamp() * 1000) - limit * timeframe_ms
        rows_by_time: dict[int, list] = {}
        while len(rows_by_time) < limit:
            batch_limit = min(300, limit - len(rows_by_time))
            batch = await self._public_request(
                self.client.fetch_ohlcv,
                client_symbol,
                timeframe=timeframe,
                since=since,
                limit=batch_limit,
            )
            if not batch:
                break
            for row in batch:
                rows_by_time[int(row[0])] = row
            next_since = int(batch[-1][0]) + timeframe_ms
            if next_since <= since:
                break
            since = next_since
            if len(batch) < batch_limit and since >= int(datetime.now(UTC).timestamp() * 1000):
                break
        rows = [rows_by_time[key] for key in sorted(rows_by_time)][-limit:]
        return [
            Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
            )
            for row in rows
        ]

    async def portfolio(self) -> PortfolioState:
        if self.public_only:
            raise PermissionError("public market adapter cannot access account balances")
        await self.client.load_markets()
        balance = await self.client.fetch_balance()
        if self.market_type == "spot":
            return await self._spot_portfolio(balance)
        return await self._swap_portfolio(balance)

    async def _spot_portfolio(self, balance: dict[str, Any]) -> PortfolioState:
        totals = balance.get("total", {})
        free = self._money(balance.get("free", {}).get("USDT", 0))
        equity = self._money(totals.get("USDT", 0))
        gross_exposure = Decimal("0")
        positions: list[Position] = []
        held_symbols = {
            currency: f"{currency}/USDT"
            for currency, raw_total in totals.items()
            if currency != "USDT"
            and raw_total is not None
            and Decimal(str(raw_total)) > 0
            and f"{currency}/USDT" in self.client.markets
        }
        tickers = (
            await self.client.fetch_tickers(list(held_symbols.values())) if held_symbols else {}
        )
        unpriced = []
        for currency, raw_total in totals.items():
            amount = self._money(raw_total or 0)
            if currency == "USDT" or amount <= 0:
                continue
            symbol = held_symbols.get(currency)
            ticker = tickers.get(symbol) if symbol else None
            if not ticker or ticker.get("last") is None:
                unpriced.append(currency)
                continue
            mark = self._money(ticker["last"])
            notional = self._money(amount * mark)
            equity += notional
            gross_exposure += notional
            positions.append(
                Position(
                    symbol=symbol,
                    side=Side.LONG,
                    quantity=amount,
                    average_entry=mark,
                    mark_price=mark,
                )
            )
        if unpriced:
            raise RuntimeError(f"账户含无法按 USDT 估值的资产：{', '.join(unpriced[:5])}")
        equity = self._money(equity)
        daily_start = self._daily_baseline(equity)
        return PortfolioState(
            equity=equity,
            cash=free,
            daily_start_equity=daily_start,
            gross_exposure=self._money(gross_exposure),
            open_positions=positions,
        )

    async def _swap_portfolio(self, balance: dict[str, Any]) -> PortfolioState:
        positions_raw = await self.client.fetch_positions()
        positions: list[Position] = []
        gross_exposure = Decimal("0")
        unrealized_total = Decimal("0")
        used_margin = Decimal("0")
        for item in positions_raw:
            contracts = Decimal(str(item.get("contracts") or 0))
            if contracts <= 0:
                continue
            contract_size = Decimal(str(item.get("contractSize") or 1))
            quantity = self._money(contracts * contract_size)
            entry = self._money(item.get("entryPrice") or 0)
            mark = self._money(item.get("markPrice") or entry)
            unrealized = self._money(item.get("unrealizedPnl") or 0)
            notional = self._money(abs(Decimal(str(item.get("notional") or quantity * mark))))
            gross_exposure += notional
            unrealized_total += unrealized
            leverage = Decimal(str(item.get("leverage") or self.contract_leverage))
            margin_used = self._money(
                item.get("initialMargin") or (notional / leverage if leverage > 0 else notional)
            )
            used_margin += margin_used
            positions.append(
                Position(
                    symbol=self._canonical_symbol(str(item["symbol"])),
                    side=(Side.SHORT if str(item.get("side")) == "short" else Side.LONG),
                    quantity=quantity,
                    average_entry=entry,
                    mark_price=mark,
                    unrealized_pnl=unrealized,
                    leverage=leverage,
                    liquidation_price=(
                        self._money(item.get("liquidationPrice"))
                        if item.get("liquidationPrice") not in (None, "")
                        else None
                    ),
                    margin_mode=str(item.get("marginMode") or self.margin_mode),
                    margin_used=margin_used,
                )
            )
        info = balance.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        okx_account: dict[str, Any] = {}
        if self.exchange_id == "okx":
            data = info.get("data") or []
            okx_account = data[0] if data else {}
            equity_raw = okx_account.get("totalEq")
        elif self.exchange_id == "binance":
            equity_raw = info.get("totalMarginBalance")
        else:
            equity_raw = None
        wallet_total = self._money(balance.get("total", {}).get("USDT", 0))
        equity = self._money(
            equity_raw if equity_raw not in (None, "") else wallet_total + unrealized_total
        )
        free = self._money(balance.get("free", {}).get("USDT", 0))
        if self.exchange_id == "okx":
            usdt_detail = next(
                (item for item in (okx_account.get("details") or []) if item.get("ccy") == "USDT"),
                {},
            )
            if usdt_detail.get("availEq") not in (None, ""):
                free = self._money(usdt_detail["availEq"])
        return PortfolioState(
            equity=equity,
            cash=free,
            daily_start_equity=self._daily_baseline(equity),
            gross_exposure=self._money(gross_exposure),
            available_margin=free,
            used_margin=self._money(used_margin),
            open_positions=positions,
        )

    @staticmethod
    def _money(value) -> Decimal:
        return Decimal(str(value or 0)).quantize(Decimal("0.00000001"))

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        return symbol.split(":", 1)[0]

    async def _resolve_symbol(self, symbol: str) -> str:
        """Map a canonical strategy symbol to the correct CCXT market variant."""
        await self._public_request(self.client.load_markets)
        markets = self.client.markets
        current = markets.get(symbol)
        if self.market_type != "swap":
            if current:
                return symbol
            raise LookupError(f"交易所不存在交易品种 {symbol}")
        if current and current.get("swap"):
            return symbol
        quote = symbol.split("/", 1)[1] if "/" in symbol else "USDT"
        settled_symbol = f"{symbol}:{quote}"
        settled = markets.get(settled_symbol)
        if settled and settled.get("swap"):
            return settled_symbol
        canonical = self._canonical_symbol(symbol)
        for candidate, market in markets.items():
            if market.get("swap") and self._canonical_symbol(candidate) == canonical:
                return candidate
        raise LookupError(f"交易所不存在 USDT 永续合约 {symbol}")

    def _daily_baseline(self, equity: Decimal) -> Decimal:
        if not self.store:
            return equity
        return self._money(self.store.daily_equity_baseline(self.account_id, equity))

    async def place_order(self, intent: OrderIntent) -> Order:
        if self.public_only:
            raise PermissionError("public market adapter cannot place orders")
        if not self.sandbox and not self.live_enabled:
            raise PermissionError("live trading is locked")
        self._validate_intent(intent)
        if self.market_type == "swap":
            await self.ensure_contract_configuration(intent.symbol, intent.position_side)
        client_symbol = await self._resolve_symbol(intent.symbol)
        amount = (
            self._venue_amount(client_symbol, intent.quantity)
            if self.market_type == "swap"
            else intent.quantity
        )
        client_id = self._venue_client_id(intent.idempotency_key, 32)
        params = {"clientOrderId": client_id, **self._order_params(intent)}

        async def submit():
            response = await self.client.create_order(
                client_symbol,
                intent.order_type.value,
                intent.side.value,
                float(amount),
                float(intent.limit_price) if intent.order_type == OrderType.LIMIT else None,
                params,
            )
            return self._map_order(response, intent.plan_id).model_copy(
                update={
                    "position_side": intent.position_side,
                    "reduce_only": intent.reduce_only,
                }
            )

        return await self._submit_order_once(client_id, intent, submit)

    @staticmethod
    def _validate_intent(intent: OrderIntent) -> None:
        if not intent.quantity.is_finite() or intent.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        if intent.order_type == OrderType.LIMIT and (
            intent.limit_price is None or intent.limit_price <= 0
        ):
            raise ValueError("limit orders require a positive limit_price")
        if intent.order_type == OrderType.MARKET and intent.limit_price is not None:
            raise ValueError("market orders must not carry limit_price")
        if intent.position_side is not None:
            opening_side = (
                OrderSide.BUY if intent.position_side == PositionSide.LONG else OrderSide.SELL
            )
            if (intent.side == opening_side) == intent.reduce_only:
                raise ValueError("side/position_side does not match opening or reducing intent")

    def _order_params(self, intent: OrderIntent) -> dict:
        params = {"reduceOnly": intent.reduce_only}
        if self.exchange_id == "okx" and self.market_type == "swap":
            params["tdMode"] = self.margin_mode
            if getattr(self, "_position_mode", "net_mode") == "long_short_mode":
                if intent.position_side is None:
                    raise ValueError("OKX long_short_mode requires explicit position_side")
                params["posSide"] = intent.position_side.value.lower()
                # OKX hedge closes are encoded by side + posSide, not reduceOnly.
                params.pop("reduceOnly")
            else:
                params["posSide"] = "net"
        elif intent.position_side is not None:
            raise ValueError("explicit position_side is supported only for OKX swaps")
        return params

    def _claim_submission(self, client_id: str, payload: dict) -> bool:
        """Durably reserve before transport. An uncertain claim is never released."""
        fingerprint = sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if not hasattr(self, "_submissions"):
            self._submissions = {}
        store = getattr(self, "store", None)
        if store is not None:
            scope = (
                f"{self.exchange_id}:{self.market_type}:{self.sandbox}:"
                f"{getattr(self, 'account_id', 'default')}"
            )
            with store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS exchange_submission_attempts "
                    "(scope TEXT, client_id TEXT, fingerprint TEXT NOT NULL, "
                    "PRIMARY KEY (scope, client_id))"
                )
                previous = connection.execute(
                    "SELECT fingerprint FROM exchange_submission_attempts "
                    "WHERE scope=? AND client_id=?",
                    (scope, client_id),
                ).fetchone()
                if previous is not None:
                    if previous[0] != fingerprint:
                        raise ValueError("idempotency key reused for a different submission")
                    return False
                connection.execute(
                    "INSERT INTO exchange_submission_attempts VALUES (?, ?, ?)",
                    (scope, client_id, fingerprint),
                )
                connection.commit()
        elif client_id in self._submissions:
            if self._submissions[client_id] != fingerprint:
                raise ValueError("idempotency key reused for a different submission")
            return False
        self._submissions[client_id] = fingerprint
        return True

    async def _submit_order_once(self, client_id, intent, submit, *, protection=None) -> Order:
        guard = getattr(self, "entry_submission_guard", None)
        if guard is not None and not intent.reduce_only:
            # Guard each child order, after all asynchronous venue preparation.
            async with guard():
                return await self._submit_order_once_guarded(
                    client_id,
                    intent,
                    submit,
                    protection=protection,
                )
        return await self._submit_order_once_guarded(
            client_id,
            intent,
            submit,
            protection=protection,
        )

    async def _submit_order_once_guarded(
        self,
        client_id,
        intent,
        submit,
        *,
        protection=None,
    ) -> Order:
        payload = intent.model_dump(mode="json", exclude={"created_at"})
        payload["protection"] = protection
        claimed = self._claim_submission(client_id, payload)
        if not claimed:
            cached = getattr(self, "_submitted_orders", {}).get(client_id)
            if cached is not None:
                return cached.model_copy(deep=True)
        error = None
        if claimed:
            try:
                order = await submit()
            except PositionProtectionError:
                raise
            except Exception as exc:
                error = exc
            else:
                if not hasattr(self, "_submitted_orders"):
                    self._submitted_orders = {}
                self._submitted_orders[client_id] = order.model_copy(deep=True)
                return order
        try:
            order = await self.find_order_by_client_id(client_id, intent.symbol)
        except Exception as exc:
            error = error or exc
            order = None
        if order is not None:
            return order.model_copy(
                update={
                    "plan_id": intent.plan_id,
                    "position_side": intent.position_side,
                    "reduce_only": intent.reduce_only,
                }
            )
        raise OrderSubmissionUncertain(
            client_id, f"submission {client_id} is uncertain; reconcile without resubmitting"
        ) from error

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        client_symbol = await self._resolve_symbol(symbol)
        await self.client.cancel_order(order_id, client_symbol)
        latest: Order | None = None
        for attempt in range(3):
            latest = await self.fetch_order(order_id, symbol)
            if latest.status in {
                OrderStatus.CANCELED,
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
            }:
                return latest
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        assert latest is not None
        return latest

    async def open_orders(self) -> list[Order]:
        responses = await self.client.fetch_open_orders()
        return [self._map_order(item, "unknown") for item in responses]

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        client_symbol = await self._resolve_symbol(symbol)
        try:
            response = await self.client.fetch_order(order_id, client_symbol)
        except Exception as exc:
            if self.exchange_id != "okx" or not self._okx_order_missing(exc):
                raise
            market = self.client.market(client_symbol)
            record = await self._okx_order_record(order_id, client_symbol)
            response = self.client.parse_order(record, market)
        return self._map_order(response, "unknown")

    async def find_order_by_client_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> Order | None:
        client_symbol = await self._resolve_symbol(symbol)
        if self.exchange_id == "okx" and hasattr(self.client, "parse_order"):
            # A market order (and its TP/SL) can finish before open-orders is read.
            market = self.client.market(client_symbol)
            response = await self.client.private_get_trade_order(
                {"instId": market["id"], "clOrdId": client_order_id}
            )
            data = response.get("data") or []
            if data:
                return self._map_order(self.client.parse_order(data[0], market), "unknown")
        responses = await self.client.fetch_open_orders(client_symbol)
        mapped = [self._map_order(item, "unknown") for item in responses]
        match = next(
            (item for item in mapped if item.client_order_id == client_order_id),
            None,
        )
        if match is not None:
            return match
        if getattr(self.client, "has", {}).get("fetchClosedOrders"):
            responses = await self.client.fetch_closed_orders(
                client_symbol,
                None,
                100,
            )
            mapped = [self._map_order(item, "unknown") for item in responses]
            return next(
                (item for item in mapped if item.client_order_id == client_order_id),
                None,
            )
        return None

    async def fetch_fills(self, symbol: str, since: datetime, limit: int = 200) -> list[Fill]:
        if self.public_only:
            raise PermissionError("private trade history is unavailable on a public adapter")
        client_symbol = await self._resolve_symbol(symbol)
        responses = await self.client.fetch_my_trades(
            client_symbol,
            int(since.timestamp() * 1000),
            min(max(limit, 1), 1000),
        )
        fills: list[Fill] = []
        for item in responses:
            timestamp = item.get("datetime") or item.get("timestamp")
            if isinstance(timestamp, (int, float)):
                filled_at = datetime.fromtimestamp(timestamp / 1000, UTC)
            else:
                filled_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if filled_at.tzinfo is None:
                    filled_at = filled_at.replace(tzinfo=UTC)
            info = item.get("info") or {}
            fee = item.get("fee") or {}
            client_order_id = str(
                item.get("clientOrderId") or info.get("clOrdId") or info.get("clientOrderId") or ""
            )
            fills.append(
                Fill(
                    id=str(item.get("id") or info.get("tradeId") or uuid4().hex),
                    order_id=str(item.get("order") or info.get("ordId") or ""),
                    client_order_id=client_order_id,
                    symbol=self._canonical_symbol(str(item.get("symbol") or symbol)),
                    side=OrderSide(str(item["side"]).lower()),
                    quantity=self._money(
                        self._base_quantity(client_symbol, Decimal(str(item["amount"])))
                    ),
                    price=self._money(item["price"]),
                    fee=self._money(fee.get("cost") or info.get("fee") or 0),
                    fee_currency=str(fee.get("currency") or "").upper(),
                    timestamp=filled_at,
                )
            )
        return fills

    async def protection_exit_order_ids(self, entry_orders: list[Order], symbol: str) -> set[str]:
        """Resolve ordinary close orders spawned by OKX attached TP/SL Algos."""

        if self.exchange_id != "okx" or self.market_type != "swap":
            return set()
        protections = await self._okx_protections(entry_orders, symbol)
        return {order_id for protection in protections for order_id in protection.exit_order_ids}

    async def reduce_position_safely(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        intent: OrderIntent,
        current_quantity: Decimal,
    ) -> Order:
        if self.exchange_id != "okx" or self.market_type != "swap":
            raise PositionProtectionError(
                "safe autonomous position reduction is only verified for OKX swaps"
            )
        if not intent.reduce_only or intent.order_type != OrderType.MARKET:
            raise PositionProtectionError("position reduction requires a reduce-only market order")
        self._validate_intent(intent)
        self._validate_position_entries(plan, entry_orders)
        expected_side = OrderSide.SELL if plan.side == Side.LONG else OrderSide.BUY
        if intent.symbol != plan.symbol or intent.side != expected_side:
            raise PositionProtectionError("reduce intent does not close the managed position")
        if intent.quantity > current_quantity or current_quantity <= 0:
            raise PositionProtectionError("reduce quantity exceeds the attributed position")
        legs = {order.position_side for order in entry_orders if order.position_side is not None}
        if intent.position_side is None and legs:
            intent = intent.model_copy(update={"position_side": next(iter(legs))})
        if intent.position_side and intent.position_side.value.lower() != str(plan.side):
            raise PositionProtectionError("reduce position_side differs from managed direction")
        await self.validate_account_configuration(require_hedge=intent.position_side is not None)
        self._order_params(intent)
        client_symbol = await self._resolve_symbol(plan.symbol)
        before = await self.portfolio()
        before_quantity = self._aligned_position_quantity(before, plan)
        if before_quantity < current_quantity:
            raise PositionProtectionError(
                "account position is smaller than the Agent-attributed quantity"
            )
        protections = await self._okx_active_protections(entry_orders, plan.symbol)
        current_venue = self._venue_amount(client_symbol, current_quantity)
        protected_venue = sum((item.size for item in protections), Decimal("0"))
        if protected_venue != current_venue:
            raise PositionProtectionError(
                "active OKX protection size does not match the Agent-attributed position"
            )
        if intent.quantity == current_quantity and before_quantity == current_quantity:
            return await self._close_okx_position_safely(
                plan,
                entry_orders,
                intent,
                client_symbol,
                before_quantity,
            )

        order: Order | None = None
        try:
            venue_client_id = self._venue_client_id(intent.idempotency_key, 32)
            order = await self.find_order_by_client_id(venue_client_id, intent.symbol)
            if order is None:
                order = await self.place_order(intent)
            for attempt in range(3):
                if order.status == OrderStatus.FILLED:
                    break
                if order.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.25 * (attempt + 1))
                order = await self.fetch_order(order.id, plan.symbol)
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                # OKX can leave a reduce-only market order live while attached
                # TP/SL children reserve the full closeable quantity. The close
                # order is already active at this point, so shrink protection to
                # the intended post-fill remainder, then confirm the venue fill.
                intended_remaining = max(
                    current_quantity - intent.quantity,
                    Decimal("0"),
                )
                await self._rebalance_okx_protections(
                    client_symbol,
                    protections,
                    intended_remaining,
                )
                for _attempt in range(8):
                    await asyncio.sleep(0.5)
                    order = await self.fetch_order(order.id, plan.symbol)
                    if order.status in {
                        OrderStatus.FILLED,
                        OrderStatus.CANCELED,
                        OrderStatus.REJECTED,
                    }:
                        break
            filled = order.filled_quantity
            expected_remaining = max(current_quantity - filled, Decimal("0"))
            await self._rebalance_okx_protections(client_symbol, protections, expected_remaining)
            after = await self.portfolio()
            after_quantity = self._aligned_position_quantity(after, plan)
            if abs((before_quantity - after_quantity) - filled) > Decimal("0.00000001"):
                raise PositionProtectionError(
                    "OKX position delta does not match the reduce-only fill", order
                )
            if order.status != OrderStatus.FILLED or filled != intent.quantity:
                raise PositionProtectionError(
                    "OKX reduce-only order did not fill the complete requested quantity",
                    order,
                )
            return order
        except PositionProtectionError as exc:
            if exc.order is not None or order is None:
                raise
            raise PositionProtectionError(str(exc), order) from exc
        except Exception as exc:
            raise PositionProtectionError(
                f"OKX protection rebalance is uncertain: {exc}", order
            ) from exc

    async def _close_okx_position_safely(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        intent: OrderIntent,
        client_symbol: str,
        before_quantity: Decimal,
    ) -> Order:
        client_order_id = self._venue_client_id(intent.idempotency_key, 32)
        self._assert_private_write()
        try:

            async def submit_close():
                response = await self.client.close_position(
                    client_symbol,
                    intent.position_side.value.lower() if intent.position_side else None,
                    {
                        "clientOrderId": client_order_id,
                        "marginMode": self.margin_mode,
                        "autoCxl": True,
                        "posSide": intent.position_side.value.lower()
                        if intent.position_side
                        else "net",
                    },
                )
                if response.get("id"):
                    return self._map_order(response, plan.id).model_copy(
                        update={
                            "position_side": intent.position_side,
                            "reduce_only": True,
                        }
                    )
                raise RuntimeError("OKX close-position acknowledgement has no order ID")

            order = await self._submit_order_once(client_order_id, intent, submit_close)
            after_quantity = before_quantity
            for _attempt in range(12):
                after = await self.portfolio()
                after_quantity = self._aligned_position_quantity(after, plan)
                if after_quantity == 0:
                    break
                await asyncio.sleep(0.5)
            if after_quantity != 0:
                if not await self.verify_position_protection(
                    plan,
                    entry_orders,
                    after_quantity,
                ):
                    raise PositionProtectionError(
                        "pending OKX close-position no longer has exact native protection",
                        order,
                    )
                return order.model_copy(
                    update={
                        "plan_id": plan.id,
                        "status": OrderStatus.OPEN,
                        "quantity": intent.quantity,
                        "updated_at": datetime.now(UTC),
                    }
                )
            if not await self.verify_position_protection(
                plan,
                entry_orders,
                Decimal("0"),
            ) and not await self.clear_position_protection(plan, entry_orders):
                raise PositionProtectionError(
                    "OKX close-position left active native protection",
                    order,
                )
            return order.model_copy(
                update={
                    "plan_id": plan.id,
                    "status": OrderStatus.FILLED,
                    "quantity": intent.quantity,
                    "filled_quantity": intent.quantity,
                    "updated_at": datetime.now(UTC),
                }
            )
        except PositionProtectionError:
            raise
        except Exception as exc:
            raise PositionProtectionError(f"OKX atomic close-position is uncertain: {exc}") from exc

    async def verify_position_protection(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        current_quantity: Decimal,
    ) -> bool:
        if self.exchange_id != "okx" or self.market_type != "swap":
            return False
        try:
            self._validate_position_entries(plan, entry_orders)
            all_protections = await self._okx_protections(entry_orders, plan.symbol)
            active = [item for item in all_protections if item.state == "live"]
            if current_quantity <= 0:
                return not active
            client_symbol = await self._resolve_symbol(plan.symbol)
            expected = self._venue_amount(client_symbol, current_quantity)
            actual = sum((item.size for item in active), Decimal("0"))
            return actual == expected
        except Exception:
            return False

    async def clear_position_protection(
        self, plan: StrategyPlan, entry_orders: list[Order]
    ) -> bool:
        if self.exchange_id != "okx" or self.market_type != "swap":
            return False
        try:
            self._assert_private_write()
            self._validate_position_entries(plan, entry_orders)
            protections = await self._okx_protections(entry_orders, plan.symbol)
            active = [item for item in protections if item.state == "live"]
            if active:
                client_symbol = await self._resolve_symbol(plan.symbol)
                await self._rebalance_okx_protections(client_symbol, active, Decimal("0"))
            return await self.verify_position_protection(plan, entry_orders, Decimal("0"))
        except Exception:
            return False

    def _assert_private_write(self) -> None:
        if self.public_only:
            raise PermissionError("public market adapter cannot place orders")
        if not self.sandbox and not self.live_enabled:
            raise PermissionError("live trading is locked")

    @staticmethod
    def _validate_position_entries(plan: StrategyPlan, entries: list[Order]) -> None:
        side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        legs = {order.position_side for order in entries}
        if len(legs) > 1 or any(
            order.symbol != plan.symbol
            or order.side != side
            or order.reduce_only
            or (order.position_side and order.position_side.value.lower() != str(plan.side))
            for order in entries
        ):
            raise PositionProtectionError("entry pool must contain one symbol and one position leg")

    @staticmethod
    def _partial_protection_id(order: Order) -> str:
        return sha256(f"partial-protection:{order.client_order_id}".encode()).hexdigest()[:32]

    async def _protect_okx_actual_fill(
        self,
        plan: StrategyPlan,
        order: Order,
        target_price: Decimal,
        *,
        exact_quantity: bool = True,
    ) -> Order:
        """Settle a partial entry before repairing it; full attachments may activate later."""
        self._assert_private_write()
        if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            try:
                latest = await self.cancel_order(order.id, order.symbol)
            except Exception as exc:
                raise PositionProtectionError(
                    "partial entry cancellation is uncertain", order
                ) from exc
            order = latest.model_copy(
                update={
                    "plan_id": order.plan_id,
                    "position_side": order.position_side,
                    "client_order_id": order.client_order_id,
                }
            )
            if order.status not in {OrderStatus.CANCELED, OrderStatus.FILLED}:
                raise PositionProtectionError("partial entry remainder is still live", order)
        if order.filled_quantity <= 0:
            return order
        symbol = await self._resolve_symbol(order.symbol)
        expected = self._venue_amount(symbol, order.filled_quantity)
        for attempt in range(3):
            protections = await self._okx_protections([order], order.symbol)
            live = [item for item in protections if item.state == "live"]
            if live:
                if exact_quantity and sum((item.size for item in live), Decimal("0")) != expected:
                    raise PositionProtectionError(
                        "entry protection coverage differs from actual fill", order
                    )
                return order
            if any(item.state in {"effective", "partially_effective"} for item in protections):
                # The entry's native exit may beat acknowledgement/recovery. Never
                # recreate a TP/SL that has already triggered; reconcile its child fills.
                return order
            if order.filled_quantity < order.quantity:
                break
            if attempt < 2:
                await asyncio.sleep(0.1)
        if order.filled_quantity >= order.quantity:
            raise PositionProtectionError(
                "filled entry native protection is not yet proven active", order
            )
        if order.status != OrderStatus.CANCELED:
            raise PositionProtectionError(
                "partial entry must be terminal before protection repair", order
            )
        client_id = self._partial_protection_id(order)
        stop = self.client.price_to_precision(symbol, float(plan.exit.stop_loss))
        target = self.client.price_to_precision(symbol, float(target_price))
        close_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
        closing = OrderIntent(
            idempotency_key=client_id,
            plan_id=plan.id,
            plan_fingerprint=plan.fingerprint,
            symbol=plan.symbol,
            side=close_side,
            position_side=order.position_side,
            order_type=OrderType.MARKET,
            quantity=order.filled_quantity,
            reduce_only=True,
        )
        params = {
            **self._order_params(closing),
            "instId": self.client.market(symbol)["id"],
            "algoClOrdId": client_id,
            "tdMode": "cash" if self.market_type == "spot" else self.margin_mode,
            "ordType": "oco",
            "side": close_side.value,
            "sz": f"{expected:f}",
            "slTriggerPx": stop,
            "slOrdPx": "-1",
            "tpTriggerPx": target,
            "tpOrdPx": "-1",
            "slTriggerPxType": "mark" if self.market_type == "swap" else "last",
            "tpTriggerPxType": "mark" if self.market_type == "swap" else "last",
        }
        error = None
        if self._claim_submission(client_id, params):
            try:
                response = await self.client.private_post_trade_order_algo(params)
                self._require_okx_batch_success(response, "repair partial-fill protection")
            except Exception as exc:
                error = exc
        try:
            protections = await self._okx_protections([order], order.symbol)
            active = [item for item in protections if item.state == "live"]
            if sum((item.size for item in active), Decimal("0")) == expected:
                return order
        except Exception as exc:
            error = error or exc
        raise OrderSubmissionUncertain(
            client_id, "partial-fill protection could not be independently confirmed", order
        ) from error

    async def rebalance_position_protection(
        self, plan: StrategyPlan, entry_orders: list[Order], current_quantity: Decimal
    ) -> bool:
        self._assert_private_write()
        if self.exchange_id != "okx" or self.market_type != "swap":
            raise PositionProtectionError("pooled protection sync requires OKX swaps")
        self._validate_position_entries(plan, entry_orders)
        if current_quantity < 0:
            raise ValueError("current_quantity cannot be negative")
        leg = next((entry.position_side for entry in entry_orders if entry.position_side), None)
        await self.validate_account_configuration(require_hedge=leg is not None)
        if self._position_mode == "long_short_mode" and leg is None:
            raise PositionProtectionError(
                "hedge protection sync requires an explicit position_side"
            )
        if current_quantity == 0:
            if not await self.clear_position_protection(plan, entry_orders):
                raise PositionProtectionError("orphaned protection cleanup is uncertain")
            return True
        settled_entries = []
        try:
            for index, entry in enumerate(entry_orders):
                if entry.filled_quantity <= 0:
                    settled_entries.append(entry)
                    continue
                target = plan.exit.take_profits[index % len(plan.exit.take_profits)]
                settled = await self._protect_okx_actual_fill(
                    plan, entry, target.price, exact_quantity=False
                )
                if settled.filled_quantity != entry.filled_quantity:
                    raise PositionProtectionError(
                        "entry filled further during cancellation; "
                        "merge its actual fill before syncing",
                        settled,
                    )
                settled_entries.append(settled)
            protections = await self._okx_active_protections(settled_entries, plan.symbol)
            symbol = await self._resolve_symbol(plan.symbol)
            expected = self._venue_amount(symbol, current_quantity)
            if sum((item.size for item in protections), Decimal("0")) < expected:
                raise PositionProtectionError("combined fills exceed verified native protection")
            stop = self.client.price_to_precision(symbol, float(plan.exit.stop_loss))
            for index, protection in enumerate(protections):
                target = self.client.price_to_precision(
                    symbol, float(plan.exit.take_profits[index % len(plan.exit.take_profits)].price)
                )
                params = {
                    "instId": self.client.market(symbol)["id"],
                    "algoId": protection.algo_id,
                    "newSlTriggerPx": stop,
                    "newSlOrdPx": "-1",
                    "newTpTriggerPx": target,
                    "newTpOrdPx": "-1",
                    "reqId": self._venue_client_id(
                        f"levels:{protection.client_id}:{stop}:{target}", 32
                    ),
                    # A failed amendment must leave the existing stop working.
                    "cxlOnFail": False,
                }
                error = None
                if protection.stop_price != stop or protection.target_price != target:
                    try:
                        response = await self.client.private_post_trade_amend_algos(params)
                        self._require_okx_batch_success(response, "synchronize TP/SL levels")
                    except Exception as exc:
                        error = exc
                detail = await self._okx_algo_detail(protection.algo_id, protection.client_id)
                if (
                    not detail
                    or detail.get("state") != "live"
                    or (
                        Decimal(str(detail.get("slTriggerPx") or 0)) != Decimal(stop)
                        or Decimal(str(detail.get("tpTriggerPx") or 0)) != Decimal(target)
                    )
                ):
                    raise PositionProtectionError(
                        "latest pooled TP/SL levels are unconfirmed"
                    ) from error
            await self._rebalance_okx_protections(symbol, protections, current_quantity)
            if not await self.verify_position_protection(plan, settled_entries, current_quantity):
                raise PositionProtectionError("pooled protection final coverage is unconfirmed")
            return True
        except PositionProtectionError:
            raise
        except Exception as exc:
            raise PositionProtectionError(
                f"pooled protection synchronization is uncertain: {exc}"
            ) from exc

    @staticmethod
    def _aligned_position_quantity(portfolio: PortfolioState, plan: StrategyPlan) -> Decimal:
        return sum(
            (
                position.quantity
                for position in portfolio.open_positions
                if position.symbol == plan.symbol and position.side == plan.side
            ),
            Decimal("0"),
        )

    async def _okx_active_protections(
        self, entry_orders: list[Order], symbol: str
    ) -> list[_OkxProtection]:
        protections = await self._okx_protections(entry_orders, symbol)
        active = [item for item in protections if item.state == "live"]
        if not active:
            raise PositionProtectionError("no active OKX protection orders were found")
        if any(item.size <= 0 for item in active):
            raise PositionProtectionError("OKX returned an active protection with no size")
        return active

    async def _okx_protections(
        self, entry_orders: list[Order], symbol: str
    ) -> list[_OkxProtection]:
        client_symbol = await self._resolve_symbol(symbol)
        identifiers: dict[str, tuple[str, str]] = {}
        expected_legs = {order.position_side for order in entry_orders}
        if len(expected_legs) > 1:
            raise PositionProtectionError("cannot mix hedge legs or legacy net orders")
        expected_leg = next(iter(expected_legs), None)
        expected_position_side = expected_leg.value.lower() if expected_leg else "net"
        expected_sides = {
            "sell" if order.side == OrderSide.BUY else "buy" for order in entry_orders
        }
        for order in entry_orders:
            if order.symbol != symbol:
                raise PositionProtectionError("protection entries must belong to one symbol")
            record = await self._okx_order_record(order.id, client_symbol)
            attachments = record.get("attachAlgoOrds") or []
            if not attachments and order.client_order_id:
                client_id = f"{order.client_order_id}p"[:32]
                identifiers[client_id] = ("", client_id)
            for item in attachments or []:
                algo_id = str(item.get("attachAlgoId") or item.get("algoId") or "")
                client_id = str(item.get("attachAlgoClOrdId") or item.get("algoClOrdId") or "")
                key = algo_id or client_id
                if key:
                    identifiers[key] = (algo_id, client_id)
            # Deterministic recovery OCO for a canceled partially filled parent.
            if order.filled_quantity > 0 and order.filled_quantity < order.quantity:
                recovery_id = self._partial_protection_id(order)
                identifiers[recovery_id] = ("", recovery_id)
        protections: list[_OkxProtection] = []
        seen: set[str] = set()
        for algo_id, client_id in identifiers.values():
            detail = await self._okx_algo_detail(algo_id, client_id)
            if not detail:
                continue
            if detail.get("instId") and detail["instId"] != self.client.market(client_symbol)["id"]:
                continue
            if detail.get("posSide", "net") != expected_position_side:
                continue
            if detail.get("side") and detail["side"] not in expected_sides:
                continue
            if expected_leg is not None and (
                not detail.get("side")
                or not detail.get("slTriggerPx")
                or not detail.get("tpTriggerPx")
            ):
                continue
            identity = str(detail.get("algoId") or detail.get("algoClOrdId") or client_id)
            if identity in seen:
                continue
            seen.add(identity)
            state = str(detail.get("state") or "").lower()
            size = Decimal(str(detail.get("sz") or 0))
            linked_order = detail.get("linkedOrd") or {}
            raw_exit_order_ids = [
                detail.get("ordId"),
                *(detail.get("ordIdList") or []),
                linked_order.get("ordId"),
            ]
            exit_order_ids = tuple(
                dict.fromkeys(str(order_id) for order_id in raw_exit_order_ids if order_id)
            )
            protections.append(
                _OkxProtection(
                    algo_id=str(detail.get("algoId") or algo_id),
                    client_id=str(detail.get("algoClOrdId") or client_id),
                    size=size,
                    state=state,
                    exit_order_ids=exit_order_ids,
                    side=str(detail.get("side") or ""),
                    position_side=str(detail.get("posSide") or "net"),
                    instrument_id=str(detail.get("instId") or ""),
                    stop_price=str(detail.get("slTriggerPx") or ""),
                    target_price=str(detail.get("tpTriggerPx") or ""),
                )
            )
        return protections

    async def _okx_algo_detail(self, algo_id: str, client_id: str) -> dict | None:
        # For attached TP/SL, OKX may expose a different Algo ID after the entry
        # becomes fully filled. The stable client Algo ID is therefore authoritative.
        identifiers = [
            {"algoClOrdId": client_id} if client_id else None,
            {"algoId": algo_id} if algo_id else None,
        ]
        for params in identifiers:
            if not params:
                continue
            try:
                response = await self.client.private_get_trade_order_algo(params)
                data = response.get("data") or []
                if data:
                    detail = data[0]
                    if str(detail.get("state") or "").lower() in {
                        "effective",
                        "partially_effective",
                    }:
                        try:
                            active = await self._okx_pending_algo(algo_id, client_id)
                        except Exception:
                            active = None
                        if active:
                            return active
                    return detail
            except Exception as exc:
                if not self._okx_order_missing(exc):
                    raise
        return await self._okx_pending_algo(algo_id, client_id)

    async def _okx_order_record(self, order_id: str, client_symbol: str) -> dict[str, Any]:
        """Read an OKX order from detail, then its authoritative 7-day history."""

        market = self.client.market(client_symbol)
        detail_error: Exception | None = None
        try:
            response = await self.client.private_get_trade_order(
                {"instId": market["id"], "ordId": order_id}
            )
            data = response.get("data") or []
            if data:
                return data[0]
        except Exception as exc:
            if not self._okx_order_missing(exc):
                raise
            detail_error = exc
        response = await self.client.private_get_trade_orders_history(
            {
                "instType": str(market.get("type") or self.market_type).upper(),
                "instId": market["id"],
                "limit": "100",
            }
        )
        record = next(
            (
                item
                for item in (response.get("data") or [])
                if str(item.get("ordId") or "") == str(order_id)
            ),
            None,
        )
        if record:
            return record
        if detail_error:
            raise detail_error
        raise LookupError(f"OKX order {order_id} is absent from detail and history")

    async def _okx_pending_algo(self, algo_id: str, client_id: str) -> dict[str, Any] | None:
        """Confirm whether an attached Algo is still active after detail returns 51603."""

        errors: list[Exception] = []
        successful_queries = 0
        for order_type in ("oco", "conditional"):
            try:
                response = await self.client.private_get_trade_orders_algo_pending(
                    {"ordType": order_type, "limit": "100"}
                )
                successful_queries += 1
            except Exception as exc:
                errors.append(exc)
                continue
            for item in response.get("data") or []:
                if (
                    algo_id
                    and str(item.get("algoId") or "") == algo_id
                    or client_id
                    and str(item.get("algoClOrdId") or "") == client_id
                ):
                    return item
        # OKX Demo can expose attached TP/SL children through the ordinary
        # open-orders response while both Algo detail and pending Algo queries
        # return no row. The stable algoClOrdId still links the child to its
        # entry, so use that venue truth instead of declaring protection absent.
        try:
            open_orders = await self.client.fetch_open_orders()
            successful_queries += 1
        except Exception as exc:
            errors.append(exc)
        else:
            for order in open_orders:
                info = dict(order.get("info") or {})
                order_algo_id = str(info.get("algoId") or order.get("id") or "")
                order_client_id = str(info.get("algoClOrdId") or order.get("clientOrderId") or "")
                if not (
                    algo_id
                    and order_algo_id == algo_id
                    or client_id
                    and order_client_id == client_id
                ):
                    continue
                info.setdefault("algoId", order_algo_id)
                info.setdefault("algoClOrdId", order_client_id)
                if str(order.get("status") or "").lower() == "open":
                    info["state"] = "live"
                else:
                    info.setdefault("state", "")
                info.setdefault("sz", order.get("amount") or "0")
                return info
        if successful_queries:
            return None
        if errors:
            raise errors[0]
        raise RuntimeError("OKX pending Algo query did not complete")

    @staticmethod
    def _okx_order_missing(exc: Exception) -> bool:
        message = str(exc)
        return "51603" in message and "Order does not exist" in message

    async def _rebalance_okx_protections(
        self,
        client_symbol: str,
        protections: list[_OkxProtection],
        remaining_base_quantity: Decimal,
    ) -> None:
        self._assert_private_write()
        market = self.client.market(client_symbol)
        remaining_venue = (
            self._venue_amount(client_symbol, remaining_base_quantity)
            if remaining_base_quantity > 0
            else Decimal("0")
        )
        allocations: list[tuple[_OkxProtection, Decimal]] = []
        unallocated = remaining_venue
        for protection in protections:
            allocation = min(protection.size, unallocated)
            allocations.append((protection, allocation))
            unallocated -= allocation
        if unallocated != 0:
            raise PositionProtectionError(
                "remaining OKX position exceeds the available protection capacity"
            )

        cancel_payload = []
        acknowledgement_errors: list[str] = []
        for protection, allocation in allocations:
            if allocation != 0:
                continue
            ordinary_child_canceled = False
            for order_id in protection.exit_order_ids:
                try:
                    await self.client.cancel_order(order_id, client_symbol)
                    ordinary_child_canceled = True
                except Exception as exc:
                    acknowledgement_errors.append(str(exc))
            if ordinary_child_canceled:
                continue
            item = {"instId": market["id"]}
            if protection.algo_id:
                item["algoId"] = protection.algo_id
            else:
                item["algoClOrdId"] = protection.client_id
            cancel_payload.append(item)
        if cancel_payload:
            try:
                response = await self.client.private_post_trade_cancel_algos(cancel_payload)
                self._require_okx_batch_success(response, "cancel protection")
            except Exception as exc:
                acknowledgement_errors.append(str(exc))
        for protection, allocation in allocations:
            if allocation <= 0 or allocation == protection.size:
                continue
            params = {
                "instId": market["id"],
                "newSz": f"{allocation:f}",
                "reqId": self._venue_client_id(f"resize:{protection.client_id}:{allocation}", 32),
            }
            if protection.algo_id:
                params["algoId"] = protection.algo_id
            else:
                params["algoClOrdId"] = protection.client_id
            try:
                response = await self.client.private_post_trade_amend_algos(params)
                self._require_okx_batch_success(response, "amend protection")
            except Exception as exc:
                acknowledgement_errors.append(str(exc))

        for attempt in range(8):
            verified = True
            for protection, allocation in allocations:
                detail = await self._okx_algo_detail(protection.algo_id, protection.client_id)
                state = str((detail or {}).get("state") or "").lower()
                size = Decimal(str((detail or {}).get("sz") or 0))
                if allocation == 0:
                    verified = verified and state != "live"
                else:
                    verified = verified and state == "live" and size == allocation
            if verified:
                return
            if attempt < 7:
                await asyncio.sleep(0.5)
        raise PositionProtectionError(
            "OKX did not confirm the amended protection quantities"
            + (f"; acknowledgement: {acknowledgement_errors[0]}" if acknowledgement_errors else "")
        )

    @staticmethod
    def _require_okx_batch_success(response: dict, operation: str) -> None:
        if str(response.get("code") or "0") != "0":
            raise PositionProtectionError(
                f"OKX failed to {operation}: {response.get('msg') or response.get('code')}"
            )
        failures = [
            item for item in (response.get("data") or []) if str(item.get("sCode") or "0") != "0"
        ]
        if failures:
            failure = failures[0]
            raise PositionProtectionError(
                f"OKX failed to {operation}: {failure.get('sMsg') or failure.get('sCode')}"
            )

    def _base_quantity(self, symbol: str, venue_quantity: Decimal) -> Decimal:
        if self.market_type != "swap":
            return venue_quantity
        market = self.client.market(symbol)
        contract_size = Decimal(str(market.get("contractSize") or 0))
        if contract_size <= 0:
            raise RuntimeError(f"{symbol} is missing a valid contract size")
        return venue_quantity * contract_size

    async def verify_protection(self, order: Order) -> bool:
        if self.exchange_id == "okx":
            if order.filled_quantity > 0:
                try:
                    protections = await self._okx_protections([order], order.symbol)
                    symbol = await self._resolve_symbol(order.symbol)
                    return sum(
                        (item.size for item in protections if item.state == "live"), Decimal("0")
                    ) == self._venue_amount(symbol, order.filled_quantity)
                except Exception:
                    return False
            client_symbol = await self._resolve_symbol(order.symbol)
            record = await self._okx_order_record(order.id, client_symbol)
            attachments = record.get("attachAlgoOrds") or []
            return bool(attachments) and all(
                item.get("failCode") in (None, "", "0")
                and item.get("tpTriggerPx")
                and item.get("slTriggerPx")
                for item in attachments
            )
        if self.exchange_id == "binance" and self.market_type == "spot":
            client_id = order.client_order_id
            if not client_id.endswith("e"):
                return False
            response = await self.client.private_get_orderlist(
                {"origClientOrderId": client_id[:-1]}
            )
            return len(response.get("orders") or []) == 3 and response.get("listOrderStatus") in {
                "EXECUTING",
                "ALL_DONE",
            }
        return False

    async def protection_available(
        self, symbol: str, position_side: PositionSide | None = None
    ) -> bool:
        if self.public_only:
            return False
        try:
            await self._resolve_symbol(symbol)
        except LookupError:
            return False
        await self.validate_account_configuration(require_hedge=position_side is not None)
        if (
            self.exchange_id == "okx"
            and self.market_type == "swap"
            and getattr(self, "_position_mode", "net_mode") == "long_short_mode"
            and position_side is None
        ):
            raise ExchangeConfigurationError(
                "okx_position_mode_not_net",
                "OKX position mode must be net_mode when no hedge leg is specified",
                "Pass an explicit LONG or SHORT position side for hedge-mode orders.",
            )
        return self.exchange_id == "okx" or (
            self.exchange_id == "binance" and self.market_type == "spot"
        )

    async def validate_account_configuration(
        self, *, require_hedge: bool = False
    ) -> dict[str, str]:
        """Fail closed when the exchange account mode cannot represent our orders."""
        if self.public_only:
            return {"status": "public_only"}
        if self.exchange_id == "okx" and self.market_type == "swap":
            response = await self.client.private_get_account_config()
            data = response.get("data") or []
            if not data:
                raise ExchangeConfigurationError(
                    "okx_account_configuration_unavailable",
                    "OKX 未返回账户配置，禁止永续合约开仓",
                    "确认 API Key 具有读取和交易权限，并在 OKX 模拟盘中完成合约账户初始化。",
                )
            configuration = data[0]
            position_mode = str(configuration.get("posMode") or "")
            if position_mode not in {"net_mode", "long_short_mode"} or (
                require_hedge and position_mode != "long_short_mode"
            ):
                raise ExchangeConfigurationError(
                    "okx_position_mode_not_hedge" if require_hedge else "okx_position_mode_unknown",
                    "OKX position mode must be long_short_mode for explicit hedge legs",
                    "Check OKX account position mode; "
                    "this adapter never switches it automatically.",
                )
            self._position_mode = position_mode
            account_level = str(configuration.get("acctLv") or "")
            if account_level == "1":
                raise ExchangeConfigurationError(
                    "okx_account_mode_spot",
                    "OKX 账户仍是现货模式，无法执行 USDT 永续合约",
                    (
                        "进入 OKX Demo Trading 的交易设置，将 Account mode "
                        "首次切换为 Futures mode，然后返回重新测试连接。"
                    ),
                )
            return {
                "status": "ok",
                "position_mode": position_mode,
                "account_level": account_level,
                "margin_mode": self.margin_mode,
                "leverage": f"{self.contract_leverage:f}",
            }
        return {"status": "ok"}

    async def ensure_contract_configuration(
        self, symbol: str, position_side: PositionSide | None = None
    ) -> None:
        if self.market_type != "swap":
            return
        if self.contract_leverage < 1:
            raise RuntimeError("合约杠杆必须大于或等于 1")
        await self.validate_account_configuration(require_hedge=position_side is not None)
        if (
            getattr(self, "_position_mode", "net_mode") == "long_short_mode"
            and position_side is None
        ):
            raise ValueError("OKX long_short_mode requires explicit position_side")
        params = {"mgnMode": self.margin_mode} if self.exchange_id == "okx" else {}
        if self.exchange_id == "okx" and self.margin_mode == "isolated" and position_side:
            params["posSide"] = position_side.value.lower()
        client_symbol = await self._resolve_symbol(symbol)
        await self.client.set_leverage(int(self.contract_leverage), client_symbol, params)

    async def minimum_commission_quantity(self, symbol: str, entry_price: Decimal) -> Decimal:
        """Return a small, venue-valid base quantity for a sandbox acceptance order."""
        client_symbol = await self._resolve_symbol(symbol)
        market = self.client.market(client_symbol)
        limits = market.get("limits") or {}
        amount_limit = (limits.get("amount") or {}).get("min")
        cost_limit = (limits.get("cost") or {}).get("min")
        minimum_base = Decimal("0")
        if amount_limit not in (None, ""):
            minimum_base = Decimal(str(amount_limit))
            if self.market_type == "swap":
                contract_size = Decimal(str(market.get("contractSize") or 0))
                if contract_size <= 0:
                    raise RuntimeError(f"{symbol} 缺少有效合约面值")
                minimum_base *= contract_size
        minimum_cost = max(Decimal(str(cost_limit or 0)), Decimal("10"))
        by_cost = minimum_cost / entry_price
        # Keep a buffer above exchange filters after venue precision truncation.
        return max(minimum_base, by_cost) * Decimal("1.10")

    async def place_protected_order(self, plan: StrategyPlan, intent: OrderIntent) -> list[Order]:
        if self.public_only:
            raise PermissionError("public market adapter cannot place orders")
        if not self.sandbox and not self.live_enabled:
            raise PermissionError("live trading is locked")
        self._validate_intent(intent)
        if intent.reduce_only or intent.symbol != plan.symbol:
            raise ValueError("protected entry must open the plan symbol")
        expected_side = OrderSide.BUY if plan.side == Side.LONG else OrderSide.SELL
        if intent.side != expected_side:
            raise ValueError("protected entry side differs from plan direction")
        if intent.position_side and intent.position_side.value.lower() != str(plan.side):
            raise ValueError("protected entry position_side differs from plan direction")
        await self.client.load_markets()
        if self.market_type == "swap":
            if plan.leverage != self.contract_leverage:
                raise RuntimeError(
                    f"策略杠杆 {plan.leverage:f}x 与执行配置 {self.contract_leverage:f}x 不一致"
                )
            if plan.margin_mode != self.margin_mode:
                raise RuntimeError(
                    f"策略保证金模式 {plan.margin_mode} 与执行配置 {self.margin_mode} 不一致"
                )
            await self.ensure_contract_configuration(plan.symbol, intent.position_side)
        if self.exchange_id == "okx":
            return await self._place_okx_protected_orders(plan, intent)
        if self.exchange_id == "binance" and self.market_type == "spot":
            if intent.order_type != OrderType.LIMIT:
                raise ValueError("Binance spot OTOCO only supports protected LIMIT entries")
            return await self._place_binance_spot_otoco(plan, intent)
        raise RuntimeError(f"{self.exchange_id} {self.market_type} 尚未验证原子保护单，禁止开仓")

    async def _place_okx_protected_orders(
        self, plan: StrategyPlan, intent: OrderIntent
    ) -> list[Order]:
        client_symbol = await self._resolve_symbol(plan.symbol)
        entry_price = (
            self.client.price_to_precision(client_symbol, float(intent.limit_price))
            if intent.order_type == OrderType.LIMIT
            else None
        )
        stop_price = self.client.price_to_precision(client_symbol, float(plan.exit.stop_loss))
        trigger_type = "mark" if self.market_type == "swap" else "last"
        submitted: list[Order] = []
        for index, (base_quantity, target) in enumerate(
            self._target_slices(plan, intent.quantity), start=1
        ):
            venue_amount = self._venue_amount(client_symbol, base_quantity)
            target_price = self.client.price_to_precision(client_symbol, float(target.price))
            client_id = f"{self._venue_client_id(intent.idempotency_key, 28)}{index}"
            params = {
                **self._order_params(intent),
                "clOrdId": client_id,
                "tdMode": "cash" if self.market_type == "spot" else self.margin_mode,
                "attachAlgoOrds": [
                    {
                        "attachAlgoClOrdId": f"{client_id}p"[:32],
                        "tpTriggerPx": target_price,
                        "tpOrdPx": "-1",
                        "slTriggerPx": stop_price,
                        "slOrdPx": "-1",
                        "tpTriggerPxType": trigger_type,
                        "slTriggerPxType": trigger_type,
                    }
                ],
            }

            async def submit(
                venue_amount=venue_amount,
                params=params,
            ):
                response = await self.client.create_order(
                    client_symbol,
                    intent.order_type.value,
                    intent.side.value,
                    float(venue_amount),
                    float(entry_price) if entry_price is not None else None,
                    params,
                )
                mapped = self._map_order(response, plan.id).model_copy(
                    update={
                        "position_side": intent.position_side,
                    }
                )
                failures = [
                    item
                    for item in ((response.get("info") or {}).get("attachAlgoOrds") or [])
                    if item.get("failCode") not in (None, "", "0")
                ]
                if failures:
                    # Retain actual fills in the exception for reconciliation.
                    if mapped.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                        mapped = await self.cancel_order(mapped.id, mapped.symbol)
                    raise PositionProtectionError("OKX rejected attached protection", mapped)
                return mapped

            mapped = await self._submit_order_once(
                client_id,
                intent,
                submit,
                protection=params["attachAlgoOrds"],
            )
            if mapped.status == OrderStatus.REJECTED:
                await self._cancel_submitted(submitted)
                raise RuntimeError("OKX 拒绝入场订单")
            if mapped.filled_quantity > 0:
                mapped = await self._protect_okx_actual_fill(plan, mapped, target.price)
            submitted.append(mapped)
        return submitted

    async def _place_binance_spot_otoco(
        self, plan: StrategyPlan, intent: OrderIntent
    ) -> list[Order]:
        market = self.client.market(plan.symbol)
        symbol_id = market["id"]
        entry_price = self.client.price_to_precision(plan.symbol, float(intent.limit_price))
        stop_price = self.client.price_to_precision(plan.symbol, float(plan.exit.stop_loss))
        submitted: list[Order] = []
        for index, (quantity, target) in enumerate(
            self._target_slices(plan, intent.quantity), start=1
        ):
            amount = self.client.amount_to_precision(plan.symbol, float(quantity))
            target_price = self.client.price_to_precision(plan.symbol, float(target.price))
            list_id = f"{self._venue_client_id(intent.idempotency_key, 31)}{index}"
            params = {
                "symbol": symbol_id,
                "listClientOrderId": list_id,
                "newOrderRespType": "RESULT",
                "workingType": "LIMIT",
                "workingSide": intent.side.value.upper(),
                "workingClientOrderId": f"{list_id}e"[:36],
                "workingPrice": entry_price,
                "workingQuantity": amount,
                "workingTimeInForce": "GTC",
                "pendingSide": ("SELL" if intent.side.value == "buy" else "BUY"),
                "pendingQuantity": amount,
                "pendingAboveType": "TAKE_PROFIT",
                "pendingAboveClientOrderId": f"{list_id}t"[:36],
                "pendingAboveStopPrice": target_price,
                "pendingBelowType": "STOP_LOSS",
                "pendingBelowClientOrderId": f"{list_id}s"[:36],
                "pendingBelowStopPrice": stop_price,
            }
            try:
                response = await self.client.private_post_orderlist_otoco(params)
            except Exception:
                existing = await self._find_binance_order_list(plan.symbol, list_id, plan.id)
                if existing:
                    submitted.append(existing)
                    continue
                await self._cancel_submitted(submitted)
                raise
            mapped = self._map_binance_order_list(response, plan.symbol, plan.id)
            if mapped.status == OrderStatus.REJECTED:
                await self._cancel_submitted(submitted)
                raise RuntimeError("Binance 拒绝 OTOCO 入场订单")
            submitted.append(mapped)
        return submitted

    def _target_slices(self, plan: StrategyPlan, total: Decimal):
        remaining = total
        targets = plan.exit.take_profits
        for index, target in enumerate(targets):
            quantity = (
                remaining
                if index == len(targets) - 1
                else min(total * target.close_fraction, remaining)
            )
            if quantity <= 0:
                raise ValueError("保护单分配后的下单数量必须大于 0")
            remaining -= quantity
            yield quantity, target

    @staticmethod
    def _venue_client_id(value: str, max_length: int) -> str:
        if not value:
            raise ValueError("client order id must not be empty")
        if len(value) <= max_length and re.fullmatch(r"[A-Za-z0-9]+", value):
            return value
        # Truncating/sanitizing loses entropy precisely where versions/actions differ.
        return sha256(value.encode()).hexdigest()[:max_length]

    def _venue_amount(self, symbol: str, base_quantity: Decimal) -> Decimal:
        market = self.client.market(symbol)
        amount = base_quantity
        if self.market_type == "swap":
            contract_size = Decimal(str(market.get("contractSize") or 0))
            if contract_size <= 0:
                raise RuntimeError(f"{symbol} 缺少有效合约面值")
            amount = base_quantity / contract_size
        precise = Decimal(self.client.amount_to_precision(symbol, float(amount)))
        if precise <= 0:
            raise ValueError(f"{symbol} 下单数量低于交易所最小精度")
        return precise

    def base_quantity_from_venue(self, instrument_id: str, venue_quantity: Decimal) -> Decimal:
        if self.market_type != "swap":
            return venue_quantity
        market = next(
            (item for item in self.client.markets.values() if item.get("id") == instrument_id),
            None,
        )
        if not market:
            raise LookupError(f"unknown exchange instrument: {instrument_id}")
        return venue_quantity * Decimal(str(market.get("contractSize") or 1))

    async def _find_okx_order(
        self, market: dict[str, Any], client_id: str, plan_id: str
    ) -> Order | None:
        try:
            response = await self.client.private_get_trade_order(
                {"instId": market["id"], "clOrdId": client_id}
            )
        except Exception:
            return None
        data = response.get("data") or []
        if not data:
            return None
        parsed = self.client.parse_order(data[0], market)
        return self._map_order(parsed, plan_id)

    async def _find_binance_order_list(
        self, symbol: str, list_id: str, plan_id: str
    ) -> Order | None:
        try:
            response = await self.client.private_get_orderlist({"origClientOrderId": list_id})
        except Exception:
            return None
        if response.get("orderReports"):
            return self._map_binance_order_list(response, symbol, plan_id)
        orders = response.get("orders") or []
        if not orders:
            return None
        try:
            parsed = await self.client.fetch_order(str(orders[0]["orderId"]), symbol)
        except Exception:
            return None
        return self._map_order(parsed, plan_id)

    def _map_binance_order_list(self, response: dict[str, Any], symbol: str, plan_id: str) -> Order:
        reports = response.get("orderReports") or []
        if not reports:
            raise RuntimeError("Binance OTOCO 响应缺少 working order")
        working = reports[0]
        status_map = {
            "NEW": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.CANCELED,
        }
        return Order(
            id=str(working["orderId"]),
            client_order_id=str(working["clientOrderId"]),
            plan_id=plan_id,
            symbol=symbol,
            side=str(working["side"]).lower(),
            order_type="limit",
            status=status_map.get(working.get("status"), OrderStatus.PENDING),
            quantity=Decimal(str(working["origQty"])),
            filled_quantity=Decimal(str(working.get("executedQty") or 0)),
            limit_price=Decimal(str(working["price"])),
            mode=TradingMode.SANDBOX if self.sandbox else TradingMode.LIVE,
        )

    async def _cancel_submitted(self, orders: list[Order]) -> None:
        for order in reversed(orders):
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                # The reconciliation worker resolves any uncertain cancellation.
                with suppress(Exception):
                    await self.cancel_order(order.id, order.symbol)

    def _map_order(self, item: dict[str, Any], plan_id: str) -> Order:
        status_map = {
            "open": OrderStatus.OPEN,
            "closed": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
        }
        quantity = Decimal(str(item.get("amount") or 0))
        filled_quantity = Decimal(str(item.get("filled") or 0))
        if self.market_type == "swap":
            market = self.client.market(str(item["symbol"]))
            contract_size = Decimal(str(market.get("contractSize") or 1))
            quantity *= contract_size
            filled_quantity *= contract_size
        info = item.get("info") or {}
        status = status_map.get(item.get("status"), OrderStatus.PENDING)
        if status == OrderStatus.OPEN and filled_quantity > 0:
            status = OrderStatus.PARTIALLY_FILLED
        raw_leg = str(info.get("posSide") or item.get("positionSide") or "").upper()

        def exchange_time(*values) -> datetime | None:
            for raw in values:
                if raw in {None, ""}:
                    continue
                if isinstance(raw, datetime):
                    return raw.astimezone(UTC)
                try:
                    numeric = float(raw)
                except (TypeError, ValueError):
                    try:
                        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(
                            UTC
                        )
                    except ValueError:
                        continue
                seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
                return datetime.fromtimestamp(seconds, tz=UTC)
            return None

        created_at = exchange_time(
            item.get("timestamp"),
            item.get("datetime"),
            info.get("cTime"),
            info.get("createTime"),
            info.get("transactTime"),
            info.get("time"),
        ) or datetime.now(UTC)
        updated_at = (
            exchange_time(
                item.get("lastTradeTimestamp"),
                info.get("uTime"),
                info.get("updateTime"),
            )
            or created_at
        )
        return Order(
            id=str(item["id"]),
            client_order_id=str(info.get("algoClOrdId") or item.get("clientOrderId") or item["id"]),
            plan_id=plan_id,
            symbol=self._canonical_symbol(str(item["symbol"])),
            side=str(item["side"]),
            position_side=PositionSide(raw_leg) if raw_leg in {"LONG", "SHORT"} else None,
            reduce_only=(
                str(info.get("reduceOnly") or item.get("reduceOnly") or "").lower() == "true"
                or (raw_leg == "LONG" and item["side"] == "sell")
                or (raw_leg == "SHORT" and item["side"] == "buy")
            ),
            order_type=str(item["type"]),
            status=status,
            quantity=self._money(quantity),
            filled_quantity=self._money(filled_quantity),
            limit_price=self._money(item["price"]) if item.get("price") else None,
            average_fill_price=(self._money(item["average"]) if item.get("average") else None),
            fee=self._money((item.get("fee") or {}).get("cost") or 0),
            mode=TradingMode.SANDBOX if self.sandbox else TradingMode.LIVE,
            created_at=created_at,
            updated_at=updated_at,
        )

    async def close(self) -> None:
        await self.client.close()
