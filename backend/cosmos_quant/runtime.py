from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter

from .config_service import ConfigurationService
from .configuration import ConnectionTestResult, UserConfiguration
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    MarketSnapshot,
    OpenInterestPoint,
    PortfolioState,
    PositioningPoint,
    TradingMode,
)
from .exchanges.base import ExchangeAdapter
from .exchanges.ccxt_adapter import CcxtExchangeAdapter, ExchangeConfigurationError
from .exchanges.paper import PaperExchange
from .market_data import MarketDataService, MarketQuality, validate_candles
from .market_evidence import MarketEvidenceSnapshot, build_market_evidence
from .order_ledger import OrderLedger, ReconciliationHealth, ReconciliationReport
from .private_order_stream import OkxPrivateOrderStream
from .risk_state import RiskStateService
from .trading_intelligence import V2_SYMBOLS
from .websocket_market import PublicTickerStream


class RuntimeManager:
    """Own market-data and execution clients and replace them atomically on config changes."""

    reconciliation_failure_threshold = 3

    def __init__(
        self,
        configuration: ConfigurationService,
        risk_state: RiskStateService | None = None,
    ):
        self.configuration_service = configuration
        self.configuration: UserConfiguration | None = None
        self.market: ExchangeAdapter | None = None
        self.execution: ExchangeAdapter | None = None
        self.market_data = MarketDataService(configuration.store)
        self.ticker_stream: PublicTickerStream | None = None
        self.ticker_streams: list[PublicTickerStream] = []
        self.risk_state = risk_state
        self.order_ledger: OrderLedger | None = None
        self.reconciliation_report: ReconciliationReport | None = None
        self._reconciliation_failures = 0
        self._reconciliation_last_success_at = None
        self._reconciliation_last_failure_at = None
        self._reconciliation_last_error = ""
        self._reconciliation_task: asyncio.Task | None = None
        self.private_order_stream: OkxPrivateOrderStream | None = None
        self.reconciliation_plan_ids_provider: Callable[[], set[str]] | None = None
        self._initialization_lock = asyncio.Lock()

    async def initialize(self, force: bool = False) -> None:
        async with self._initialization_lock:
            await self._initialize_locked(force)

    async def _initialize_locked(self, force: bool = False) -> None:
        current = self.configuration_service.current_configuration()
        if not force and self.configuration == current and self.market and self.execution:
            return
        await self._close_clients()
        credentials = self.configuration_service.credentials_for(current)
        if current.trading_mode == TradingMode.PAPER:
            self.market = CcxtExchangeAdapter(
                current.exchange_id,
                {},
                sandbox=False,
                live_enabled=False,
                public_only=True,
                market_type=current.market_type,
                margin_mode=current.margin_mode,
                contract_leverage=current.contract_leverage,
                proxy_url=current.network_proxy_url,
            )
            self.execution = PaperExchange(
                initial_cash=current.paper_equity,
                fee_bps=current.fee_bps,
                slippage_bps=current.slippage_bps,
                market_type=current.market_type,
                margin_mode=current.margin_mode,
                contract_leverage=current.contract_leverage,
                store=self.configuration_service.store,
                account_id=f"paper:{current.exchange_id}:{current.market_type}",
            )
        else:
            self._validate_private_configuration(current, credentials)
            adapter = CcxtExchangeAdapter(
                current.exchange_id,
                credentials,
                sandbox=current.trading_mode == TradingMode.SANDBOX,
                live_enabled=(
                    current.trading_mode == TradingMode.LIVE and current.live_trading_enabled
                ),
                public_only=False,
                market_type=current.market_type,
                margin_mode=current.margin_mode,
                contract_leverage=current.contract_leverage,
                store=self.configuration_service.store,
                account_id=(f"{current.exchange_id}:{current.trading_mode}:{current.market_type}"),
                proxy_url=current.network_proxy_url,
            )
            self.market = adapter
            self.execution = adapter
        self.configuration = current
        ledger_key = f"{current.exchange_id}:{current.trading_mode}:{current.market_type}"
        self.order_ledger = OrderLedger(self.configuration_service.store, ledger_key)
        stream_timeframes = list(
            dict.fromkeys([current.default_timeframe, *current.strategy.timeframes])
        )
        stream_symbols = list(
            dict.fromkeys([
                current.default_symbol,
                *current.strategy.watchlist,
                *(symbol.removesuffix(":USDT") for symbol in V2_SYMBOLS),
            ])
        )
        self.ticker_streams = [
            PublicTickerStream(
                current.exchange_id,
                symbol,
                current.market_type,
                self.market_data.update_websocket,
                proxy_url=current.network_proxy_url,
                timeframe=current.default_timeframe,
                timeframes=stream_timeframes,
                candle_callback=self.market_data.update_exchange_candle,
            )
            for symbol in stream_symbols
        ]
        self.ticker_stream = self.ticker_streams[0] if self.ticker_streams else None
        for stream in self.ticker_streams:
            stream.start()
        # Do not strand initialization on one private REST timeout. New entries
        # remain blocked until a clean reconciliation pass succeeds.
        await self.reconcile_orders(raise_on_error=False)
        if (
            current.trading_mode != TradingMode.PAPER
            and current.exchange_id == "okx"
            and isinstance(self.execution, CcxtExchangeAdapter)
        ):
            self.private_order_stream = OkxPrivateOrderStream(
                credentials,
                sandbox=current.trading_mode == TradingMode.SANDBOX,
                quantity_converter=self.execution.base_quantity_from_venue,
                callback=self._handle_private_order_event,
                proxy_url=current.network_proxy_url,
            )
            self.private_order_stream.start()
        self._reconciliation_task = asyncio.create_task(
            self._reconciliation_loop(),
            name="private-order-reconciliation",
        )

    @staticmethod
    def _validate_private_configuration(
        configuration: UserConfiguration, credentials: dict[str, str]
    ) -> None:
        if not credentials.get("api_key") or not credentials.get("secret"):
            raise RuntimeError("当前交易模式需要先在设置中填写 API Key 和 Secret")
        if configuration.exchange_id == "okx" and not credentials.get("passphrase"):
            raise RuntimeError("OKX 账户需要填写 API Passphrase")
        if (
            configuration.trading_mode == TradingMode.LIVE
            and not configuration.live_trading_enabled
        ):
            raise RuntimeError("实盘模式尚未通过设置页的风险解锁")

    async def market_context(
        self,
        limit: int = 500,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> tuple[MarketSnapshot, list[Candle]]:
        await self.initialize()
        assert self.market is not None
        assert self.configuration is not None
        selected_symbol = symbol or self.configuration.default_symbol
        selected_timeframe = timeframe or self.configuration.default_timeframe
        market, history, _ = await self.market_data.fetch(
            self.market,
            self.configuration.exchange_id,
            selected_symbol,
            selected_timeframe,
            limit,
        )
        if self.configuration.market_type == "swap":
            funding = None
            with suppress(Exception):
                funding = await self.market.funding_snapshot(selected_symbol)
            if funding:
                market = market.model_copy(update=funding)
            microstructure = None
            with suppress(Exception):
                microstructure = await self.market.market_microstructure(selected_symbol)
            if microstructure:
                open_interest_update = {
                    key: microstructure[key]
                    for key in (
                        "open_interest",
                        "open_interest_value",
                        "open_interest_change_24h",
                        "long_short_ratio",
                        "long_account_fraction",
                        "short_account_fraction",
                        "positioning_time",
                    )
                    if microstructure.get(key) is not None
                }
                trade_flow = microstructure.get("trade_flow_imbalance")
                if trade_flow is not None:
                    observed_at = microstructure.get("observed_at") or market.timestamp
                    quote_volume = microstructure.get("trade_flow_quote")
                    open_interest_update.update(
                        {
                            "order_flow_imbalance": trade_flow,
                            "order_flow_time": observed_at,
                            "order_flow_quote_volume": quote_volume,
                            "order_flow_delta": (
                                Decimal(str(trade_flow)) * Decimal(str(quote_volume))
                                if quote_volume is not None
                                else None
                            ),
                        }
                    )
                if open_interest_update:
                    if (
                        open_interest_update.get("open_interest") is not None
                        or open_interest_update.get("open_interest_value") is not None
                    ):
                        open_interest_update["open_interest_time"] = (
                            microstructure.get("observed_at") or market.timestamp
                        )
                    market = market.model_copy(update=open_interest_update)
        if len(history) < 57:
            raise RuntimeError("交易所返回的历史 K 线不足 57 根")
        if isinstance(self.execution, PaperExchange):
            self.execution.seed_candles(market.symbol, history)
            self.execution.update_market(market)
        return market, history

    async def portfolio(self) -> PortfolioState:
        await self.initialize()
        assert self.execution is not None
        return await self.execution.portfolio()

    async def market_evidence(
        self,
        symbol: str,
        timeframe: str,
        *,
        market: MarketSnapshot | None = None,
        candles: list[Candle] | None = None,
    ) -> MarketEvidenceSnapshot:
        """Combine closed-candle state with optional venue microstructure evidence."""

        await self.initialize()
        assert self.market is not None
        if market is None or candles is None:
            market, candles = await self.market_context(
                limit=300,
                symbol=symbol,
                timeframe=timeframe,
            )
        microstructure = None
        with suppress(Exception):
            microstructure = await self.market.market_microstructure(symbol)
        return build_market_evidence(
            candles,
            market,
            timeframe,
            microstructure,
        )

    async def funding_history(
        self,
        symbol: str,
        start,
        end,
    ) -> list[FundingRatePoint]:
        await self.initialize()
        assert self.market is not None
        return await self.market.fetch_funding_history(symbol, start, end)

    async def open_interest_history(
        self,
        symbol: str,
        timeframe: str,
        start,
        end,
    ) -> list[OpenInterestPoint]:
        await self.initialize()
        assert self.market is not None
        return await self.market.fetch_open_interest_history(
            symbol,
            timeframe,
            start,
            end,
        )

    async def positioning_history(
        self,
        symbol: str,
        timeframe: str,
        start,
        end,
    ) -> list[PositioningPoint]:
        await self.initialize()
        assert self.market is not None
        return await self.market.fetch_positioning_history(
            symbol,
            timeframe,
            start,
            end,
        )

    async def basis_history(
        self,
        symbol: str,
        timeframe: str,
        start,
        end,
    ) -> list[BasisPoint]:
        await self.initialize()
        assert self.market is not None
        return await self.market.fetch_basis_history(
            symbol,
            timeframe,
            start,
            end,
        )

    async def test_connection(
        self,
        configuration: UserConfiguration,
        credentials_override: dict[str, str] | None = None,
    ) -> ConnectionTestResult:
        started = perf_counter()
        credentials = credentials_override or self.configuration_service.credentials_for(
            configuration
        )
        private_required = configuration.trading_mode != TradingMode.PAPER
        adapter = CcxtExchangeAdapter(
            configuration.exchange_id,
            credentials if private_required else {},
            sandbox=(configuration.trading_mode == TradingMode.SANDBOX),
            live_enabled=False,
            public_only=not private_required,
            market_type=configuration.market_type,
            margin_mode=configuration.margin_mode,
            contract_leverage=configuration.contract_leverage,
            proxy_url=configuration.network_proxy_url,
        )
        try:
            snapshot = await adapter.market_snapshot(configuration.default_symbol)
            candles = await adapter.fetch_candles(
                configuration.default_symbol, configuration.default_timeframe, 60
            )
            private_ok = None
            protected_order_ok = configuration.trading_mode == TradingMode.PAPER
            if private_required:
                try:
                    self._validate_private_configuration(configuration, credentials)
                    await adapter.portfolio()
                    await adapter.validate_account_configuration()
                    private_ok = True
                    protected_order_ok = await adapter.protection_available(
                        configuration.default_symbol
                    )
                except Exception as exc:
                    latency = round((perf_counter() - started) * 1000)
                    if isinstance(exc, ExchangeConfigurationError):
                        failure_code = exc.code
                        required_action = exc.required_action
                    else:
                        failure_code = "private_account_unavailable"
                        required_action = (
                            "检查当前运行模式对应的 API 凭证、权限和交易所账户状态后重新测试。"
                        )
                    return ConnectionTestResult(
                        ok=False,
                        exchange=configuration.exchange_id,
                        mode=configuration.trading_mode,
                        public_market_ok=True,
                        private_account_ok=False,
                        protected_order_ok=False,
                        latency_ms=latency,
                        symbol=configuration.default_symbol,
                        last_price=snapshot.last,
                        candle_count=len(candles),
                        failure_code=failure_code,
                        required_action=required_action,
                        message=str(exc),
                    )
            latency = round((perf_counter() - started) * 1000)
            return ConnectionTestResult(
                ok=bool(not private_required or (private_ok and protected_order_ok)),
                exchange=configuration.exchange_id,
                mode=configuration.trading_mode,
                public_market_ok=True,
                private_account_ok=private_ok,
                protected_order_ok=protected_order_ok,
                latency_ms=latency,
                symbol=configuration.default_symbol,
                last_price=snapshot.last,
                candle_count=len(candles),
                failure_code=(
                    "protected_order_unavailable" if private_ok and not protected_order_ok else None
                ),
                required_action=(
                    "当前交易所和市场未通过原子保护单能力检查，禁止开仓；"
                    "请改用已验收的 OKX USDT 永续路径。"
                    if private_ok and not protected_order_ok
                    else None
                ),
                message=(
                    "公共行情、账户与原子保护单能力均正常"
                    if private_ok and protected_order_ok
                    else (
                        "账户可连接，但当前交易所/市场没有已验证的原子保护单，禁止开仓"
                        if private_ok
                        else "公共行情连接正常"
                    )
                ),
            )
        finally:
            await adapter.close()

    async def close(self) -> None:
        async with self._initialization_lock:
            await self._close_clients()

    async def _close_clients(self) -> None:
        if self.private_order_stream:
            await self.private_order_stream.close()
            self.private_order_stream = None
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconciliation_task
            self._reconciliation_task = None
        streams = self.ticker_streams or ([self.ticker_stream] if self.ticker_stream else [])
        if streams:
            await asyncio.gather(*(stream.close() for stream in streams))
        self.ticker_streams = []
        self.ticker_stream = None
        clients = {id(client): client for client in (self.market, self.execution) if client}
        for client in clients.values():
            close = getattr(client, "close", None)
            if close:
                await close()
        self.market = None
        self.execution = None
        self.order_ledger = None
        self.reconciliation_report = None
        self._reconciliation_failures = 0
        self._reconciliation_last_success_at = None
        self._reconciliation_last_failure_at = None
        self._reconciliation_last_error = ""

    def _handle_private_order_event(self, event) -> None:
        if self.order_ledger and not self.order_ledger.apply_event(event):
            asyncio.create_task(self.reconcile_orders(raise_on_error=False))

    async def reconcile_orders(self, *, raise_on_error: bool = True) -> ReconciliationReport | None:
        if not self.execution or not self.order_ledger:
            return None
        try:
            if self.reconciliation_plan_ids_provider is None:
                report = await self.order_ledger.reconcile(self.execution)
            else:
                report = await self.order_ledger.reconcile(
                    self.execution,
                    protected_plan_ids=self.reconciliation_plan_ids_provider(),
                )
        except Exception as exc:
            self._reconciliation_failures += 1
            self._reconciliation_last_failure_at = datetime.now(UTC)
            self._reconciliation_last_error = str(exc)[:300]
            if (
                self.risk_state
                and self._reconciliation_failures >= self.reconciliation_failure_threshold
            ):
                self.risk_state.halt(
                    f"订单对账连续失败 {self._reconciliation_failures} 次：{str(exc)[:160]}",
                    source="reconciliation_transport",
                )
            if raise_on_error:
                raise
            return None
        self._reconciliation_failures = 0
        self._reconciliation_last_success_at = report.checked_at
        self._reconciliation_last_error = ""
        self.reconciliation_report = report
        has_integrity_failure = bool(
            report.uncertain_order_ids
            or report.unknown_open_order_ids
            or report.unknown_position_symbols
            or report.unprotected_order_ids
        )
        if self.risk_state:
            if has_integrity_failure:
                self.risk_state.halt(
                    "订单状态不确定、存在外部挂单或保护单缺失，需人工核对",
                    source="reconciliation_integrity",
                )
            else:
                recover = getattr(
                    self.risk_state,
                    "recover_reconciliation_transport",
                    None,
                )
                if recover:
                    recover()
                if (
                    self.configuration
                    and self.configuration.trading_mode == TradingMode.SANDBOX
                ):
                    recover_demo_integrity = getattr(
                        self.risk_state,
                        "recover_demo_reconciliation_integrity",
                        None,
                    )
                    if recover_demo_integrity:
                        recover_demo_integrity()
        return report

    async def _reconciliation_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self.reconcile_orders(raise_on_error=False)

    @property
    def reconciliation_health(self) -> ReconciliationHealth:
        healthy = self._reconciliation_failures == 0
        return ReconciliationHealth(
            healthy=healthy,
            entry_allowed=healthy and self.reconciliation_report is not None,
            consecutive_failures=self._reconciliation_failures,
            failure_threshold=self.reconciliation_failure_threshold,
            last_success_at=self._reconciliation_last_success_at,
            last_failure_at=self._reconciliation_last_failure_at,
            last_error=self._reconciliation_last_error,
        )

    @property
    def reconciliation_entry_allowed(self) -> bool:
        return self.reconciliation_health.entry_allowed

    @property
    def market_quality(self) -> MarketQuality | None:
        return self.market_data.quality

    @property
    def private_stream_status(self):
        return self.private_order_stream.status if self.private_order_stream else None


class NetworkFreeRuntime(RuntimeManager):
    """Network-free runtime used only by automated tests."""

    __test__ = False

    def __init__(
        self,
        configuration: ConfigurationService,
        candles: list[Candle],
        snapshot: MarketSnapshot,
    ):
        super().__init__(configuration)
        self._candles = candles
        self._snapshot = snapshot

    async def initialize(self, force: bool = False) -> None:
        del force
        current = self.configuration_service.current_configuration()
        if self.market and self.execution and self.configuration == current:
            return
        paper = PaperExchange(
            initial_cash=current.paper_equity,
            market_type=current.market_type,
            margin_mode=current.margin_mode,
            contract_leverage=current.contract_leverage,
        )
        paper.seed_candles(self._snapshot.symbol, self._candles)
        paper.update_market(self._snapshot)
        self.market = paper
        self.execution = paper
        self.configuration = current
        ledger_key = f"test:{current.exchange_id}:{current.trading_mode}:{current.market_type}"
        self.order_ledger = OrderLedger(self.configuration_service.store, ledger_key)
        _, quality = validate_candles(self._candles, current.default_timeframe)
        quality.source = "test:verified-fixture"
        self.market_data.quality = quality

    async def market_context(
        self,
        limit: int = 500,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> tuple[MarketSnapshot, list[Candle]]:
        del timeframe
        await self.initialize()
        if symbol and symbol != self._snapshot.symbol:
            raise LookupError(f"no test market for {symbol}")
        return self._snapshot, self._candles[-limit:]
