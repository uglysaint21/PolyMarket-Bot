"""Deterministic paper execution against explicit quote snapshots; no network or orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


def dec(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    market_id: str
    timestamp: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuoteSnapshot":
        q = cls(
            market_id=str(data["market_id"]),
            timestamp=int(data["timestamp"]),
            bid=dec(data["bid"], "bid"),
            ask=dec(data["ask"], "ask"),
            bid_size=dec(data["bid_size"], "bid_size"),
            ask_size=dec(data["ask_size"], "ask_size"),
        )
        if not q.market_id or q.timestamp <= 0:
            raise ValueError("market_id and positive quote timestamp are required")
        if not (Decimal("0") <= q.bid <= q.ask <= Decimal("1")):
            raise ValueError("quote must satisfy 0 <= bid <= ask <= 1")
        if q.bid_size < 0 or q.ask_size < 0:
            raise ValueError("quote depth cannot be negative")
        return q


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_size: Decimal = Decimal("10")
    max_position_size: Decimal = Decimal("25")
    max_notional: Decimal = Decimal("10")
    max_quote_age_seconds: int = 5
    fee_rate: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    kill_switch: bool = False

    def __post_init__(self) -> None:
        if self.max_order_size <= 0 or self.max_position_size <= 0 or self.max_notional <= 0:
            raise ValueError("order, position, and notional limits must be positive")
        if self.max_quote_age_seconds < 0 or self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("age, fee, and slippage parameters cannot be negative")
        if self.fee_rate >= 1 or self.slippage_bps >= 10_000:
            raise ValueError("fee_rate must be < 1 and slippage_bps < 10000")


@dataclass
class PaperPortfolio:
    cash: Decimal = Decimal("100")
    positions: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = dec(self.cash, "cash")
        if self.cash < 0:
            raise ValueError("initial cash cannot be negative")
        self.positions = {key: dec(value, "position") for key, value in self.positions.items()}
        if any(value < 0 for value in self.positions.values()):
            raise ValueError("seeded positions cannot be negative")


class PaperEngine:
    """All-or-none fills; supplied quotes are assumed to represent executable depth."""

    def __init__(self, portfolio: PaperPortfolio, limits: RiskLimits) -> None:
        self.portfolio = portfolio
        self.limits = limits

    def execute(self, order: dict[str, Any], quote: QuoteSnapshot, *, now: int) -> dict[str, Any]:
        side = str(order.get("side", "")).upper()
        market_id = str(order.get("market_id", ""))
        try:
            quantity = dec(order.get("quantity"), "quantity")
            created_at = int(order.get("timestamp"))
        except (ValueError, TypeError) as exc:
            return {"status": "rejected", "reason": str(exc)}
        if side not in {"BUY", "SELL"} or not market_id:
            return {"status": "rejected", "reason": "side must be BUY/SELL and market_id is required"}
        if quantity <= 0:
            return {"status": "rejected", "reason": "quantity must be positive"}
        if self.limits.kill_switch:
            return {"status": "rejected", "reason": "kill switch enabled"}
        if quote.market_id != market_id:
            return {"status": "rejected", "reason": "quote market_id does not match order"}
        age = now - quote.timestamp
        if age < 0 or age > self.limits.max_quote_age_seconds:
            return {"status": "rejected", "reason": "quote is stale or from the future"}
        if created_at > now or now - created_at > self.limits.max_quote_age_seconds:
            return {"status": "rejected", "reason": "order intent is stale or from the future"}
        if quantity > self.limits.max_order_size:
            return {"status": "rejected", "reason": "max order size exceeded"}
        position = self.portfolio.positions.get(market_id, Decimal("0"))
        if side == "BUY":
            if position + quantity > self.limits.max_position_size:
                return {"status": "rejected", "reason": "max position size exceeded"}
            if quantity > quote.ask_size:
                return {"status": "rejected", "reason": "insufficient displayed ask depth"}
            price = quote.ask * (Decimal("1") + self.limits.slippage_bps / Decimal("10000"))
            if price > 1:
                return {"status": "rejected", "reason": "slippage-adjusted price exceeds binary token bound"}
        else:
            if quantity > position:
                return {"status": "rejected", "reason": "sell exceeds reconciled paper inventory"}
            if quantity > quote.bid_size:
                return {"status": "rejected", "reason": "insufficient displayed bid depth"}
            price = quote.bid * (Decimal("1") - self.limits.slippage_bps / Decimal("10000"))
        notional = price * quantity
        if notional > self.limits.max_notional:
            return {"status": "rejected", "reason": "max notional exceeded"}
        fee = notional * self.limits.fee_rate
        if side == "BUY":
            total_cost = notional + fee
            if total_cost > self.portfolio.cash:
                return {"status": "rejected", "reason": "insufficient paper cash"}
            self.portfolio.cash -= total_cost
            self.portfolio.positions[market_id] = position + quantity
        else:
            self.portfolio.cash += notional - fee
            remaining = position - quantity
            if remaining:
                self.portfolio.positions[market_id] = remaining
            else:
                self.portfolio.positions.pop(market_id, None)
        return {
            "status": "filled",
            "market_id": market_id,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
            "notional": str(notional),
            "fee": str(fee),
            "cash_after": str(self.portfolio.cash),
            "position_after": str(self.portfolio.positions.get(market_id, Decimal("0"))),
            "quote_age_seconds": age,
            "assumptions": "All-or-none paper fill at supplied top-of-book price with configured slippage/fee; no queue position, latency, hidden depth, or partial fills.",
        }
