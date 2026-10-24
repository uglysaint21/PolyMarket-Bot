"""Validated normalized records for observed public trade rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A normalized observation; not a complete position or accounting entry."""

    event_id: str
    wallet: str
    timestamp: int
    side: str
    asset_id: str
    condition_id: str | None
    price: Decimal
    size: Decimal
    title: str | None
    outcome: str | None
    raw_json: str

    @property
    def notional_usdc(self) -> Decimal:
        return self.price * self.size

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, wallet_hint: str = "") -> "TradeEvent":
        if not isinstance(raw, dict):
            raise ValueError("trade row must be an object")
        try:
            timestamp = int(raw["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("timestamp is missing or invalid") from exc
        # Some upstream fixtures may use milliseconds; normalize to seconds.
        if timestamp > 100_000_000_000:
            timestamp //= 1000
        if timestamp <= 0:
            raise ValueError("timestamp must be positive")
        side = str(raw.get("side", "")).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        asset_id = str(raw.get("asset", raw.get("asset_id", ""))).strip()
        if not asset_id:
            raise ValueError("asset/asset_id is missing")
        price = _decimal(raw.get("price"), "price")
        size = _decimal(raw.get("size"), "size")
        if not Decimal("0") <= price <= Decimal("1"):
            raise ValueError("price must be between 0 and 1")
        if size <= 0:
            raise ValueError("size must be positive")
        wallet = str(raw.get("proxyWallet", raw.get("proxy_wallet", wallet_hint))).strip().lower()
        if not wallet:
            raise ValueError("wallet is missing and no wallet hint was supplied")
        condition = raw.get("conditionId", raw.get("condition_id"))
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            event_id=event_id,
            wallet=wallet,
            timestamp=timestamp,
            side=side,
            asset_id=asset_id,
            condition_id=str(condition) if condition is not None else None,
            price=price,
            size=size,
            title=str(raw["title"]) if raw.get("title") is not None else None,
            outcome=str(raw["outcome"]) if raw.get("outcome") is not None else None,
            raw_json=canonical,
        )
