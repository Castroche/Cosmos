from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from ..domain import (
    BasisPoint,
    Candle,
    Fill,
    FundingRatePoint,
    MarketSnapshot,
    OpenInterestPoint,
    Order,
    OrderIntent,
    PortfolioState,
    PositioningPoint,
    StrategyPlan,
)


class PositionProtectionError(RuntimeError):
    """A reduce-only order may have landed but its remaining protection is uncertain."""

    def __init__(self, message: str, order: Order | None = None):
        super().__init__(message)
        self.order = order


class ExchangeAdapter(ABC):
    """Boundary for paper, sandbox, and live exchange implementations."""

    @abstractmethod
    async def health(self) -> dict[str, str | bool]: ...

    @abstractmethod
    async def market_snapshot(self, symbol: str) -> MarketSnapshot: ...

    async def funding_snapshot(self, symbol: str) -> dict | None:
        """Return normalized perpetual funding data when the venue supports it."""
        del symbol
        return None

    async def market_microstructure(self, symbol: str) -> dict | None:
        """Return normalized public order-book, trade-flow and open-interest evidence."""
        del symbol
        return None

    async def fetch_funding_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[FundingRatePoint]:
        """Return normalized historical funding without substituting spot data."""
        del symbol, start, end
        return []

    async def fetch_open_interest_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[OpenInterestPoint]:
        """Return normalized historical derivatives positioning data."""
        del symbol, timeframe, start, end
        return []

    async def fetch_positioning_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[PositioningPoint]:
        """Return exchange-published long/short positioning observations."""
        del symbol, timeframe, start, end
        return []

    async def fetch_basis_history(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[BasisPoint]:
        """Return synchronized historical mark/index basis observations."""
        del symbol, timeframe, start, end
        return []

    async def fetch_fills(self, symbol: str, since: datetime, limit: int = 200) -> list[Fill]:
        """Return private account fills; adapters must not synthesize missing history."""
        del symbol, since, limit
        return []

    async def protection_exit_order_ids(self, entry_orders: list[Order], symbol: str) -> set[str]:
        """Return venue-proven child order IDs created by native protection.

        Native TP/SL implementations may replace the submitted Algo identifier with
        one or more ordinary exchange order IDs when a trigger fires. The default is
        deliberately empty; adapters must only return IDs proven by venue metadata.
        """

        del entry_orders, symbol
        return set()

    async def reduce_position_safely(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        intent: OrderIntent,
        current_quantity: Decimal,
    ) -> Order:
        """Reduce risk and prove remaining native protection before returning."""
        del plan, entry_orders, intent, current_quantity
        raise PositionProtectionError(
            "this exchange adapter has no verified protection-rebalance protocol"
        )

    async def verify_position_protection(
        self,
        plan: StrategyPlan,
        entry_orders: list[Order],
        current_quantity: Decimal,
    ) -> bool:
        """Prove that native protection exactly covers an attributed position."""
        del plan, entry_orders, current_quantity
        return False

    async def clear_position_protection(
        self, plan: StrategyPlan, entry_orders: list[Order]
    ) -> bool:
        del plan, entry_orders
        return False

    @abstractmethod
    async def fetch_candles(
        self, symbol: str, timeframe: str = "1h", limit: int = 500
    ) -> list[Candle]: ...

    @abstractmethod
    async def portfolio(self) -> PortfolioState: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> Order: ...

    async def find_order_by_client_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> Order | None:
        """Recover an ambiguously acknowledged order without resubmitting it."""

        return next(
            (
                order
                for order in await self.open_orders()
                if order.client_order_id == client_order_id and order.symbol == symbol
            ),
            None,
        )

    @abstractmethod
    async def verify_protection(self, order: Order) -> bool: ...

    @abstractmethod
    async def protection_available(self, symbol: str) -> bool: ...

    @abstractmethod
    async def place_protected_order(
        self, plan: StrategyPlan, intent: OrderIntent
    ) -> list[Order]: ...
