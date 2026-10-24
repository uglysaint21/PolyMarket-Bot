from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from polymarket_bot import cli
from polymarket_bot.data_api import PolymarketDataClient
from polymarket_bot.store import ResearchStore

WALLET = "0x" + "ab" * 20
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "paper-orders.jsonl"


def run_cli(*argv: str) -> int:
    with pytest.raises(SystemExit) as info:
        cli.main(list(argv))
    code = info.value.code
    assert isinstance(code, int)
    return code


def trade_row(timestamp: int = 100, **overrides) -> dict:
    row = {"timestamp": timestamp, "side": "BUY", "asset": "t", "price": "0.4", "size": "4", "proxyWallet": WALLET}
    row.update(overrides)
    return row


def page(rows: list[dict], next_cursor: str | None = None) -> httpx.Response:
    body = {"data": rows, "pagination": {"has_more": next_cursor is not None, "next_cursor": next_cursor}}
    return httpx.Response(200, json=body)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch):
    """Route the CLI's client through an httpx MockTransport; returns a setter for the handler."""
    state: dict = {"handler": lambda request: page([])}

    def factory(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: state["handler"](request)))
        return PolymarketDataClient(client=http, max_retries=0, **kwargs)

    monkeypatch.setattr(cli, "PolymarketDataClient", factory)
    return lambda handler: state.__setitem__("handler", handler)


# --- argument validation ---------------------------------------------------------------------


@pytest.mark.parametrize("wallet", ["", "0xabc", "abc" * 14, "0x" + "g" * 40, "0x" + "a" * 41])
def test_bad_wallets_are_rejected_by_the_parser(wallet: str, tmp_path: Path) -> None:
    assert run_cli("report", "--wallet", wallet, "--db", str(tmp_path / "x.db")) == 2


def test_wallet_is_lowercased_by_the_parser() -> None:
    args = cli._parser().parse_args(["report", "--wallet", WALLET.upper().replace("0X", "0x")])
    assert args.wallet == WALLET


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", "1e9999999"])
def test_paper_sim_rejects_bad_decimals_with_usage_error_not_traceback(bad: str, tmp_path: Path) -> None:
    for flag in ("--initial-cash", "--max-order-size", "--fee-rate", "--slippage-bps"):
        code = run_cli("paper-sim", "--input", str(FIXTURE), "--output", str(tmp_path / "o.jsonl"), flag, bad)
        assert code == 2


# --- paper-sim -------------------------------------------------------------------------------


def test_paper_sim_end_to_end_on_the_shipped_fixture(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out" / "results.jsonl"
    code = run_cli("paper-sim", "--input", str(FIXTURE), "--output", str(out), "--initial-cash", "100")
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["orders"], summary["filled"], summary["rejected"]) == (3, 2, 1)
    results = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["status"] for r in results] == ["filled", "filled", "rejected"]
    assert "inventory" in results[2]["reason"]


def test_paper_sim_survives_hostile_lines_and_reports_each(tmp_path: Path, capsys) -> None:
    quote = '{"market_id":"m","timestamp":100,"bid":"0.4","ask":"0.5","bid_size":"5","ask_size":"5"}'
    lines = [
        "not json",
        "[1, 2]",
        "5",
        '{"timestamp": Infinity, "market_id": "m", "side": "BUY", "quantity": "1", "quote": ' + quote + "}",
        '{"timestamp": 100, "market_id": "m", "side": "BUY", "quantity": "1e9999999", "quote": ' + quote + "}",
        '{"timestamp": 100, "market_id": "m", "side": "BUY", "quantity": "1"}',
        "",
        '{"timestamp": 100, "market_id": "m", "side": "BUY", "quantity": "1", "quote": ' + quote + "}",
    ]
    source = tmp_path / "in.jsonl"
    source.write_text("\n".join(lines) + "\n")
    out = tmp_path / "out.jsonl"
    assert run_cli("paper-sim", "--input", str(source), "--output", str(out)) == 0
    results = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["status"] for r in results] == ["rejected"] * 6 + ["filled"]
    assert json.loads(capsys.readouterr().out)["orders"] == 7  # blank line skipped


def test_paper_sim_failure_leaves_no_partial_output(tmp_path: Path, capsys) -> None:
    out = tmp_path / "results.jsonl"
    out.write_text("previous good result\n")
    code = run_cli("paper-sim", "--input", str(tmp_path / "missing.jsonl"), "--output", str(out))
    assert code == 1
    assert out.read_text() == "previous good result\n"
    assert list(tmp_path.glob(".results.jsonl.*")) == []  # temp file cleaned up
    assert "polybot:" in capsys.readouterr().err


def test_paper_sim_invalid_limits_exit_1(tmp_path: Path, capsys) -> None:
    code = run_cli("paper-sim", "--input", str(FIXTURE), "--output", str(tmp_path / "o"), "--fee-rate", "1")
    assert code == 1
    assert "fee_rate" in capsys.readouterr().err


# --- export-trades ---------------------------------------------------------------------------


def test_export_writes_jsonl_across_pages(fake_api, tmp_path: Path, capsys) -> None:
    fake_api(lambda request: page([trade_row(2)]) if request.url.params.get("cursor") else page([trade_row(1)], "c1"))
    out = tmp_path / "trades.jsonl"
    assert run_cli("export-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--output", str(out)) == 0
    assert [json.loads(line)["timestamp"] for line in out.read_text().splitlines()] == [1, 2]
    assert "Exported 2" in capsys.readouterr().out


def test_failed_export_leaves_no_truncated_file(fake_api, tmp_path: Path) -> None:
    fake_api(lambda request: httpx.Response(500) if request.url.params.get("cursor") else page([trade_row()], "c1"))
    out = tmp_path / "trades.jsonl"
    assert run_cli("export-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--output", str(out)) == 1
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


# --- sync-trades -----------------------------------------------------------------------------


def runs_for(db: Path) -> list[dict]:
    with ResearchStore(db) as store:
        return store.ingestion_runs(WALLET)


def test_sync_ingests_and_summarizes(fake_api, tmp_path: Path, capsys) -> None:
    fake_api(lambda request: page([trade_row(1), trade_row(1), trade_row(2), {"timestamp": "bad"}]))
    db = tmp_path / "r.sqlite3"
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["status"], summary["received"], summary["inserted"], summary["invalid"]) == ("succeeded", 4, 2, 1)
    # Re-running is idempotent: same rows are all duplicates.
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db)) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 0


def test_sync_flushes_in_batches(fake_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli, "SYNC_BATCH_SIZE", 2)
    batches: list[int] = []
    original = ResearchStore.record_rows

    def spy(self, run_id, rows):
        rows = list(rows)
        batches.append(len(rows))
        return original(self, run_id, rows)

    monkeypatch.setattr(ResearchStore, "record_rows", spy)
    fake_api(lambda request: page([trade_row(t) for t in range(1, 6)]))
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(tmp_path / "r.db")) == 0
    assert batches == [2, 2, 1]
    assert json.loads(capsys.readouterr().out)["inserted"] == 5


def test_api_failure_marks_the_run_failed(fake_api, tmp_path: Path, capsys) -> None:
    fake_api(lambda request: httpx.Response(400))
    db = tmp_path / "r.sqlite3"
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db)) == 1
    (run,) = runs_for(db)
    assert run["status"] == "failed"
    assert "HTTP 400" in run["error"]
    assert "run_id=" in capsys.readouterr().err


def test_database_error_marks_the_run_failed_instead_of_leaving_it_running(
    fake_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: sqlite3.Error is not an OSError/ValueError/RuntimeError, so runs stayed 'running'."""

    def boom(self, run_id, rows):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ResearchStore, "record_rows", boom)
    fake_api(lambda request: page([trade_row()]))
    db = tmp_path / "r.sqlite3"
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db)) == 1
    (run,) = runs_for(db)
    assert run["status"] == "failed"
    assert "OperationalError" in run["error"]


def test_keyboard_interrupt_finalizes_the_run_and_propagates(
    fake_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(self, run_id, rows):
        raise KeyboardInterrupt

    monkeypatch.setattr(ResearchStore, "record_rows", interrupted)
    fake_api(lambda request: page([trade_row()]))
    db = tmp_path / "r.sqlite3"
    with pytest.raises(KeyboardInterrupt):
        cli.main(["sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db)])
    (run,) = runs_for(db)
    assert (run["status"], run["error"]) == ("failed", "interrupted")


def test_sync_rejects_bad_window_without_creating_a_run(tmp_path: Path) -> None:
    db = tmp_path / "r.sqlite3"
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "10", "--end", "5", "--db", str(db)) == 2
    assert not db.exists()


def test_sync_refuses_a_non_https_base_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("POLYMARKET_DATA_API_BASE_URL", "http://insecure.example/v2")
    db = tmp_path / "r.sqlite3"
    assert run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "10", "--db", str(db)) == 2
    assert "https" in capsys.readouterr().err
    assert not db.exists()


# --- report ----------------------------------------------------------------------------------


def test_report_on_missing_database_does_not_create_one(tmp_path: Path, capsys) -> None:
    db = tmp_path / "typo" / "r.sqlite3"
    assert run_cli("report", "--wallet", WALLET, "--db", str(db)) == 1
    assert "not found" in capsys.readouterr().err
    assert not db.parent.exists()


def test_report_outputs_activity_and_recent_runs(fake_api, tmp_path: Path, capsys) -> None:
    fake_api(lambda request: page([trade_row(1), trade_row(2, side="SELL")]))
    db = tmp_path / "r.sqlite3"
    run_cli("sync-trades", "--wallet", WALLET, "--start", "1", "--end", "1000", "--db", str(db))
    capsys.readouterr()
    assert run_cli("report", "--wallet", WALLET, "--db", str(db)) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["observed_trade_rows"], report["buy_rows"], report["sell_rows"]) == (2, 1, 1)
    assert report["recent_ingestion_runs"][0]["status"] == "succeeded"


@pytest.mark.parametrize("extra", [["--start", "0"], ["--end", "-1"], ["--start", "9", "--end", "3"]])
def test_report_rejects_bad_windows(extra: list[str], tmp_path: Path) -> None:
    assert run_cli("report", "--wallet", WALLET, "--db", str(tmp_path / "r.db"), *extra) == 2
