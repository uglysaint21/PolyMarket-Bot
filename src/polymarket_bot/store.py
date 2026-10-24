"""Idempotent SQLite storage for normalized public trade observations."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import TradeEvent


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    event_id TEXT PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    asset_id TEXT NOT NULL,
                    condition_id TEXT,
                    price TEXT NOT NULL,
                    size TEXT NOT NULL,
                    title TEXT,
                    outcome TEXT,
                    raw_json TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trades_wallet_time
                    ON trades(wallet, timestamp);
                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    wallet TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    received INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    invalid INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
                    error TEXT
                );
            """)

    def start_run(self, wallet: str) -> str:
        run_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO ingestion_runs(run_id,wallet,started_at,status) VALUES(?,?,?,'running')",
                (run_id, wallet.lower(), int(time.time())),
            )
        return run_id

    def record_row(self, run_id: str, raw: dict[str, Any]) -> str:
        with self._connect() as db:
            run = db.execute("SELECT wallet,status FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("ingestion run is missing or not running")
            try:
                event = TradeEvent.from_raw(raw, wallet_hint=run["wallet"])
            except (ValueError, TypeError):
                db.execute("UPDATE ingestion_runs SET received=received+1, invalid=invalid+1 WHERE run_id=?", (run_id,))
                return "invalid"
            cursor = db.execute(
                """INSERT OR IGNORE INTO trades
                (event_id,wallet,timestamp,side,asset_id,condition_id,price,size,title,outcome,raw_json,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.event_id,event.wallet,event.timestamp,event.side,event.asset_id,event.condition_id,
                 str(event.price),str(event.size),event.title,event.outcome,event.raw_json,int(time.time())),
            )
            inserted = cursor.rowcount == 1
            db.execute(
                "UPDATE ingestion_runs SET received=received+1, inserted=inserted+? WHERE run_id=?",
                (int(inserted), run_id),
            )
            return "inserted" if inserted else "duplicate"

    def finish_run(self, run_id: str, *, error: str | None = None) -> None:
        with self._connect() as db:
            status = "failed" if error else "succeeded"
            db.execute(
                "UPDATE ingestion_runs SET finished_at=?, status=?, error=? WHERE run_id=? AND status='running'",
                (int(time.time()), status, error[:1000] if error else None, run_id),
            )

    def run_summary(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def report(self, wallet: str, *, start: int | None = None, end: int | None = None) -> dict[str, Any]:
        clauses = ["wallet=?"]
        params: list[Any] = [wallet.strip().lower()]
        if start is not None:
            clauses.append("timestamp>=?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp<=?")
            params.append(end)
        where = " AND ".join(clauses)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT side,price,size,timestamp FROM trades WHERE {where} ORDER BY timestamp",
                params,
            ).fetchall()
        buy_count = sum(row["side"] == "BUY" for row in rows)
        sell_count = len(rows) - buy_count
        shares = sum((Decimal(row["size"]) for row in rows), Decimal("0"))
        notional = sum((Decimal(row["price"]) * Decimal(row["size"]) for row in rows), Decimal("0"))
        return {
            "wallet": wallet.strip().lower(),
            "start": start,
            "end": end,
            "observed_trade_rows": len(rows),
            "buy_rows": buy_count,
            "sell_rows": sell_count,
            "observed_share_volume": str(shares),
            "observed_gross_notional_usdc": str(notional),
            "accounting_warning": "Observed trade rows are not a complete position ledger; these metrics are not P&L or realized return.",
        }

    def events(self, wallet: str, *, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
        clauses = ["wallet=?"]
        params: list[Any] = [wallet.strip().lower()]
        if start is not None:
            clauses.append("timestamp>=?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp<=?")
            params.append(end)
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_id,wallet,timestamp,side,asset_id,condition_id,price,size,title,outcome FROM trades WHERE "
                + " AND ".join(clauses) + " ORDER BY timestamp,event_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def ingestion_runs(self, wallet: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id,started_at,finished_at,received,inserted,invalid,status,error FROM ingestion_runs WHERE wallet=? ORDER BY started_at DESC LIMIT ?",
                (wallet.strip().lower(), limit),
            ).fetchall()
        return [dict(row) for row in rows]
