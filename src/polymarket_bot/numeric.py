"""Strict decimal parsing shared by the ingestion and paper-trading layers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# Anything beyond this magnitude is not a plausible price, share count or cash balance.
# Without a bound, a value such as "1e9999999" is finite and parses fine, but later
# arithmetic (sums, price * size) raises decimal.Overflow and breaks every report that
# touches the stored row.
MAX_MAGNITUDE = Decimal("1e15")


def parse_decimal(value: Any, name: str) -> Decimal:
    """Parse ``value`` into a finite, bounded Decimal or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    # copy_abs(), not abs(): abs() applies the arithmetic context and itself raises
    # decimal.Overflow for exponents like 1e9999999, which is exactly what we are screening for.
    if result.copy_abs() >= MAX_MAGNITUDE:
        raise ValueError(f"{name} magnitude is out of range")
    return result


def parse_int(value: Any, name: str) -> int:
    """Parse ``value`` into an int or raise ValueError.

    Covers the cases a bare ``int()`` leaks as other exception types: ``None`` and containers
    (TypeError) and ``float("inf")`` (OverflowError, reachable because ``json.loads`` accepts
    ``Infinity``). Booleans are rejected so ``true`` is not silently read as ``1``.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is missing or invalid") from exc
