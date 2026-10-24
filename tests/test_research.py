from __future__ import annotations

import json
from decimal import Decimal

import pytest

from polymarket_bot.models import TradeEvent
from polymarket_bot.paper import PaperEngine, PaperPortfolio, QuoteSnapshot, RiskLimits
from polymarket_bot.store import ResearchStore


def trade(timestamp: int = 100, side: str = "BUY", size: str = "4") -> dict:
    return {
        "timestamp": timestamp,
        "side": side,
        "asset": "token-yes",
        "conditionId": "condition-1",
        "price": "0.4",
        "size": size,
        "proxyWallet": "0xabc",
        "title": "Will event happen?",
        "outcome": "Yes",
    }


def test_normalization_and_stable_identity() -> None:
    first = TradeEvent.from_raw(trade())
    second = TradeEvent.from_raw(trade())
    assert first.event_id == second.event_id
    assert first.wallet == "0xabc"
    assert first.timestamp == 100
    assert first.notional_usdc == Decimal("1.6")


def test_invalid_trade_fields_fail_closed() -> None:
    row = trade()
    row["price"] = "1.2"
    with pytest.raises(ValueError, match="price"):
        TradeEvent.from_raw(row)


def test_sqlite_ingestion_is_idempotent_and_reports_observations(tmp_path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    run_id = store.start_run("0xABC")
    assert store.record_row(run_id, trade()) == "inserted"
    assert store.record_row(run_id, trade()) == "duplicate"
    assert store.record_row(run_id, {"timestamp": "bad"}) == "invalid"
    store.finish_run(run_id)
    summary = store.run_summary(run_id)
    assert (summary["received"], summary["inserted"], summary["invalid"], summary["status"]) == (3, 1, 1, "succeeded")
    report = store.report("0xabc")
    assert report["observed_trade_rows"] == 1
    assert report["observed_gross_notional_usdc"] == "1.6"
    assert "not P&L" in report["accounting_warning"]


def test_paper_buy_and_sell_apply_fees_and_inventory_checks() -> None:
    portfolio = PaperPortfolio(Decimal("20"))
    engine = PaperEngine(portfolio, RiskLimits(
        max_order_size=Decimal("5"), max_position_size=Decimal("5"),
        max_notional=Decimal("10"), fee_rate=Decimal("0.01"),
        slippage_bps=Decimal("100"), max_quote_age_seconds=5,
    ))
    quote = QuoteSnapshot.from_dict({
        "market_id": "token-yes", "timestamp": 100, "bid": "0.4", "ask": "0.5",
        "bid_size": "5", "ask_size": "5",
    })
    buy = engine.execute({"market_id": "token-yes", "side": "BUY", "quantity": "2", "timestamp": 100}, quote, now=101)
    assert buy["status"] == "filled"
    assert Decimal(buy["price"]) == Decimal("0.505")
    assert Decimal(buy["fee"]) == Decimal("0.01010")
    sell = engine.execute({"market_id": "token-yes", "side": "SELL", "quantity": "1", "timestamp": 101}, quote, now=101)
    assert sell["status"] == "filled"
    assert Decimal(sell["position_after"]) == Decimal("1")
    too_much = engine.execute({"market_id": "token-yes", "side": "SELL", "quantity": "2", "timestamp": 101}, quote, now=101)
    assert too_much["status"] == "rejected"
    assert "inventory" in too_much["reason"]


@pytest.mark.parametrize("mutation,reason", [
    ({"kill_switch": True}, "kill switch"),
    ({"max_order_size": Decimal("1")}, "max order size"),
    ({"max_position_size": Decimal("1")}, "max position size"),
    ({"max_notional": Decimal("0.1")}, "max notional"),
])
def test_risk_controls_reject_order(mutation, reason) -> None:
    config = {
        "max_order_size": Decimal("5"), "max_position_size": Decimal("5"),
        "max_notional": Decimal("10"), "max_quote_age_seconds": 5,
    }
    config.update(mutation)
    engine = PaperEngine(PaperPortfolio(Decimal("20")), RiskLimits(**config))
    quote = QuoteSnapshot.from_dict({
        "market_id": "token-yes", "timestamp": 100, "bid": "0.4", "ask": "0.5",
        "bid_size": "5", "ask_size": "5",
    })
    result = engine.execute({"market_id": "token-yes", "side": "BUY", "quantity": "2", "timestamp": 100}, quote, now=101)
    assert result["status"] == "rejected"
    assert reason in result["reason"]


def test_stale_quote_and_insufficient_depth_reject_without_state_change() -> None:
    portfolio = PaperPortfolio(Decimal("20"))
    engine = PaperEngine(portfolio, RiskLimits(max_quote_age_seconds=2))
    stale = QuoteSnapshot.from_dict({
        "market_id": "token-yes", "timestamp": 100, "bid": "0.4", "ask": "0.5",
        "bid_size": "1", "ask_size": "1",
    })
    result = engine.execute({"market_id": "token-yes", "side": "BUY", "quantity": "2", "timestamp": 102}, stale, now=103)
    assert result["status"] == "rejected"
    assert portfolio.cash == Decimal("20")
    assert portfolio.positions == {}


def test_quote_validation_rejects_crossed_book() -> None:
    with pytest.raises(ValueError, match="quote"):
        QuoteSnapshot.from_dict({
            "market_id": "token-yes", "timestamp": 100, "bid": "0.7", "ask": "0.5",
            "bid_size": "1", "ask_size": "1",
        })
