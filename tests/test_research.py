from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from polymarket_bot.models import TradeEvent
from polymarket_bot.paper import PaperEngine, PaperPortfolio, QuoteSnapshot, RiskLimits
from polymarket_bot.store import SCHEMA_VERSION, ResearchStore


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
    with ResearchStore(tmp_path / "research.sqlite3") as store:
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


# --- store: connection lifecycle -------------------------------------------------------------


def test_in_memory_store_is_usable() -> None:
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xABC")
        assert store.record_row(run_id, trade()) == "inserted"
        store.finish_run(run_id)
        assert store.report("0xabc")["observed_trade_rows"] == 1


def test_store_close_releases_the_connection(tmp_path) -> None:
    store = ResearchStore(tmp_path / "r.sqlite3")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.start_run("0xabc")


def test_file_store_uses_wal_and_records_schema_version(tmp_path) -> None:
    with ResearchStore(tmp_path / "r.sqlite3") as store:
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_store_refuses_a_newer_schema(tmp_path) -> None:
    path = tmp_path / "r.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        ResearchStore(path)


def test_reopening_a_store_keeps_data(tmp_path) -> None:
    path = tmp_path / "r.sqlite3"
    with ResearchStore(path) as store:
        run_id = store.start_run("0xabc")
        store.record_row(run_id, trade())
        store.finish_run(run_id)
    with ResearchStore(path) as store:
        assert store.report("0xabc")["observed_trade_rows"] == 1


# --- store: batching and run state -----------------------------------------------------------


def test_record_rows_counts_each_outcome_in_one_transaction() -> None:
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xabc")
        counts = store.record_rows(run_id, [trade(100), trade(100), trade(101), {"timestamp": "bad"}])
        assert counts == {"inserted": 2, "duplicate": 1, "invalid": 1}
        store.finish_run(run_id)
        summary = store.run_summary(run_id)
        assert (summary["received"], summary["inserted"], summary["invalid"]) == (4, 2, 1)


def test_record_rows_requires_a_running_run() -> None:
    with ResearchStore(":memory:") as store:
        with pytest.raises(RuntimeError, match="not running"):
            store.record_rows("no-such-run", [trade()])
        run_id = store.start_run("0xabc")
        store.finish_run(run_id)
        with pytest.raises(RuntimeError, match="not running"):
            store.record_rows(run_id, [trade()])


def test_finish_run_records_error_and_is_not_overwritten() -> None:
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xabc")
        store.finish_run(run_id, error="boom")
        store.finish_run(run_id)  # a second finish must not flip failed -> succeeded
        summary = store.run_summary(run_id)
        assert (summary["status"], summary["error"]) == ("failed", "boom")


def test_start_run_normalizes_wallet() -> None:
    with ResearchStore(":memory:") as store:
        store.start_run("  0xABC ")
        assert len(store.ingestion_runs("0xabc")) == 1


def test_window_filters_and_ordering() -> None:
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xabc")
        store.record_rows(run_id, [trade(300), trade(100), trade(200, side="SELL")])
        stamps = [event["timestamp"] for event in store.events("0xabc")]
        assert stamps == [100, 200, 300]
        assert [e["timestamp"] for e in store.events("0xabc", start=150, end=250)] == [200]
        report = store.report("0xabc", start=150, end=250)
        assert (report["observed_trade_rows"], report["sell_rows"], report["buy_rows"]) == (1, 1, 0)
        with pytest.raises(ValueError, match="start must be <= end"):
            store.report("0xabc", start=5, end=1)


# --- hostile / malformed input ---------------------------------------------------------------


@pytest.mark.parametrize("field,value", [("size", "1e9999999"), ("size", "1e15")])
def test_out_of_range_numbers_are_invalid_rows(field: str, value: str) -> None:
    row = trade()
    row[field] = value
    with pytest.raises(ValueError):
        TradeEvent.from_raw(row)


def test_oversized_size_cannot_poison_reports() -> None:
    """Regression: a stored size of 1e9999999 made every later `report` raise decimal.Overflow."""
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xabc")
        assert store.record_row(run_id, trade(size="1e9999999")) == "invalid"
        assert store.record_row(run_id, trade()) == "inserted"
        assert store.report("0xabc")["observed_trade_rows"] == 1


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), None, True, [1], "1e400", "x"])
def test_unusable_timestamps_are_invalid_not_crashes(bad) -> None:
    row = trade()
    row["timestamp"] = bad
    with ResearchStore(":memory:") as store:
        run_id = store.start_run("0xabc")
        assert store.record_row(run_id, row) == "invalid"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", "abc", None, True])
def test_unusable_sizes_are_rejected(bad) -> None:
    row = trade()
    row["size"] = bad
    with pytest.raises(ValueError):
        TradeEvent.from_raw(row)


def test_millisecond_timestamps_are_normalized_but_identity_tracks_raw_row() -> None:
    seconds = TradeEvent.from_raw(trade(timestamp=1_700_000_000))
    millis = TradeEvent.from_raw(trade(timestamp=1_700_000_000_000))
    assert seconds.timestamp == millis.timestamp == 1_700_000_000
    assert seconds.event_id != millis.event_id  # documented: identity hashes the whole source row


def test_wallet_falls_back_to_hint_and_is_lowercased() -> None:
    row = trade()
    del row["proxyWallet"]
    assert TradeEvent.from_raw(row, wallet_hint="0xABC").wallet == "0xabc"
    with pytest.raises(ValueError, match="wallet"):
        TradeEvent.from_raw(row)


# --- paper engine ----------------------------------------------------------------------------


def _quote(**overrides) -> QuoteSnapshot:
    data = {"market_id": "m", "timestamp": 100, "bid": "0.4", "ask": "0.5", "bid_size": "5", "ask_size": "5"}
    data.update(overrides)
    return QuoteSnapshot.from_dict(data)


def _order(**overrides) -> dict:
    order = {"market_id": "m", "side": "BUY", "quantity": "1", "timestamp": 100}
    order.update(overrides)
    return order


def test_zero_priced_buy_is_rejected_instead_of_filling_for_free() -> None:
    """Regression: ask == 0 passed every check and filled at price 0."""
    portfolio = PaperPortfolio(Decimal("10"))
    result = PaperEngine(portfolio, RiskLimits()).execute(_order(), _quote(bid="0", ask="0"), now=100)
    assert result["status"] == "rejected"
    assert "positive" in result["reason"]
    assert portfolio.cash == Decimal("10")
    assert portfolio.positions == {}


def test_zero_priced_sell_is_rejected() -> None:
    portfolio = PaperPortfolio(Decimal("10"), {"m": Decimal("3")})
    result = PaperEngine(portfolio, RiskLimits()).execute(_order(side="SELL"), _quote(bid="0", ask="0.1"), now=100)
    assert result["status"] == "rejected"
    assert portfolio.positions == {"m": Decimal("3")}


def test_slippage_pushing_buy_above_one_is_rejected() -> None:
    engine = PaperEngine(PaperPortfolio(Decimal("10")), RiskLimits(slippage_bps=Decimal("500")))
    result = engine.execute(_order(), _quote(bid="0.97", ask="0.98"), now=100)
    assert result["status"] == "rejected"
    assert "bound" in result["reason"]


def test_missing_or_garbage_order_timestamp_is_a_rejection_not_a_crash() -> None:
    engine = PaperEngine(PaperPortfolio(Decimal("10")), RiskLimits())
    for bad in (None, float("inf"), "x", True):
        order = _order()
        order["timestamp"] = bad
        assert engine.execute(order, _quote(), now=100)["status"] == "rejected"
    order = _order()
    del order["timestamp"]
    assert engine.execute(order, _quote(), now=100)["status"] == "rejected"


def test_stale_and_future_order_intents_are_rejected() -> None:
    engine = PaperEngine(PaperPortfolio(Decimal("10")), RiskLimits(max_quote_age_seconds=5))
    assert "intent" in engine.execute(_order(timestamp=90), _quote(timestamp=100), now=101)["reason"]
    assert "intent" in engine.execute(_order(timestamp=200), _quote(timestamp=100), now=101)["reason"]


def test_future_quote_is_rejected() -> None:
    engine = PaperEngine(PaperPortfolio(Decimal("10")), RiskLimits())
    assert "stale or from the future" in engine.execute(_order(), _quote(timestamp=200), now=100)["reason"]


def test_quote_for_a_different_market_is_rejected() -> None:
    engine = PaperEngine(PaperPortfolio(Decimal("10")), RiskLimits())
    result = engine.execute(_order(market_id="other"), _quote(), now=100)
    assert "does not match" in result["reason"]


def test_insufficient_cash_and_depth_leave_state_unchanged() -> None:
    portfolio = PaperPortfolio(Decimal("0.1"))
    engine = PaperEngine(portfolio, RiskLimits())
    assert "cash" in engine.execute(_order(quantity="1"), _quote(), now=100)["reason"]
    assert "depth" in engine.execute(_order(quantity="6"), _quote(ask_size="1"), now=100)["reason"]
    assert (portfolio.cash, portfolio.positions) == (Decimal("0.1"), {})


def test_selling_the_whole_position_removes_it() -> None:
    portfolio = PaperPortfolio(Decimal("0"), {"m": Decimal("2")})
    result = PaperEngine(portfolio, RiskLimits()).execute(_order(side="SELL", quantity="2"), _quote(), now=100)
    assert result["status"] == "filled"
    assert portfolio.positions == {}
    assert portfolio.cash == Decimal("0.8")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_order_size": Decimal("NaN")},
        {"max_notional": Decimal("Infinity")},
        {"fee_rate": Decimal("NaN")},
        {"slippage_bps": Decimal("NaN")},
        {"fee_rate": Decimal("1")},
        {"slippage_bps": Decimal("10000")},
        {"max_order_size": Decimal("0")},
        {"max_quote_age_seconds": -1},
    ],
)
def test_invalid_risk_limits_raise_value_error(kwargs: dict) -> None:
    """NaN used to escape as decimal.InvalidOperation from an ordered comparison."""
    with pytest.raises(ValueError):
        RiskLimits(**kwargs)


@pytest.mark.parametrize("bad", ["NaN", "-1", "1e9999999", "abc"])
def test_invalid_portfolio_cash_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        PaperPortfolio(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [{"bid": "-0.1"}, {"ask": "1.1"}, {"bid_size": "-1"}, {"market_id": ""}, {"timestamp": 0}, {"timestamp": float("inf")}],
)
def test_invalid_quotes_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _quote(**overrides)
