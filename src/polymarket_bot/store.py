"""Idempotent SQLite storage for normalized public trade observations."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

from .models import TradeEvent

SCHEMA_VERSION = 1


class ResearchStore:
    """SQLite-backed store holding one connection for its lifetime.

    A single connection (rather than one per call) is required for ``":memory:"`` databases,
    which are private to the connection that created them, and avoids a connect + fsync per row.
    Use it as a context manager, or call :meth:`close`.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=15)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=15000")
            if self.path != ":memory:":
                # WAL lets `report` read while a sync is writing; NORMAL is durable enough for
                # re-fetchable public data (a crash can lose the last commit, never corrupt).
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ResearchStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _initialize(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )
        with self._db:
            self._db.executescript("""
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
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _normalize_wallet(wallet: str) -> str:
        return wallet.strip().lower()

    def start_run(self, wallet: str) -> str:
        run_id = str(uuid.uuid4())
        with self._db:
            self._db.execute(
                "INSERT INTO ingestion_runs(run_id,wallet,started_at,status) VALUES(?,?,?,'running')",
                (run_id, self._normalize_wallet(wallet), int(time.time())),
            )
        return run_id

    def record_rows(self, run_id: str, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Record a batch of raw rows in one transaction; returns counts by outcome."""
        counts = {"inserted": 0, "duplicate": 0, "invalid": 0}
        with self._db:
            run = self._db.execute(
                "SELECT wallet,status FROM ingestion_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RuntimeError("ingestion run is missing or not running")
            now = int(time.time())
            for raw in rows:
                try:
                    event = TradeEvent.from_raw(raw, wallet_hint=run["wallet"])
                except (ValueError, TypeError):
                    counts["invalid"] += 1
                    continue
                cursor = self._db.execute(
                    """INSERT OR IGNORE INTO trades
                    (event_id,wallet,timestamp,side,asset_id,condition_id,price,size,title,outcome,raw_json,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event.event_id, event.wallet, event.timestamp, event.side, event.asset_id,
                     event.condition_id, str(event.price), str(event.size), event.title,
                     event.outcome, event.raw_json, now),
                )
                counts["inserted" if cursor.rowcount == 1 else "duplicate"] += 1
            self._db.execute(
                "UPDATE ingestion_runs SET received=received+?, inserted=inserted+?, invalid=invalid+? WHERE run_id=?",
                (sum(counts.values()), counts["inserted"], counts["invalid"], run_id),
            )
        return counts

    def record_row(self, run_id: str, raw: dict[str, Any]) -> str:
        """Record one raw row; returns 'inserted', 'duplicate' or 'invalid'."""
        counts = self.record_rows(run_id, [raw])
        return next(outcome for outcome, count in counts.items() if count)

    def finish_run(self, run_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "succeeded"
        with self._db:
            self._db.execute(
                "UPDATE ingestion_runs SET finished_at=?, status=?, error=? WHERE run_id=? AND status='running'",
                (int(time.time()), status, error[:1000] if error else None, run_id),
            )

    def run_summary(self, run_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def _window(self, wallet: str, start: int | None, end: int | None) -> tuple[str, list[Any]]:
        if start is not None and end is not None and start > end:
            raise ValueError("start must be <= end")
        clauses = ["wallet=?"]
        params: list[Any] = [self._normalize_wallet(wallet)]
        if start is not None:
            clauses.append("timestamp>=?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp<=?")
            params.append(end)
        return " AND ".join(clauses), params

    def report(self, wallet: str, *, start: int | None = None, end: int | None = None) -> dict[str, Any]:
        where, params = self._window(wallet, start, end)
        buy_count = sell_count = 0
        shares = Decimal("0")
        notional = Decimal("0")
        # Stream rows instead of materializing the whole window in memory.
        for row in self._db.execute(f"SELECT side,price,size FROM trades WHERE {where}", params):
            if row["side"] == "BUY":
                buy_count += 1
            else:
                sell_count += 1
            size = Decimal(row["size"])
            shares += size
            notional += Decimal(row["price"]) * size
        return {
            "wallet": self._normalize_wallet(wallet),
            "start": start,
            "end": end,
            "observed_trade_rows": buy_count + sell_count,
            "buy_rows": buy_count,
            "sell_rows": sell_count,
            "observed_share_volume": str(shares),
            "observed_gross_notional_usdc": str(notional),
            "accounting_warning": (
                "Observed trade rows are not a complete position ledger; "
                "these metrics are not P&L or realized return."
            ),
        }

    def events(self, wallet: str, *, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
        where, params = self._window(wallet, start, end)
        rows = self._db.execute(
            "SELECT event_id,wallet,timestamp,side,asset_id,condition_id,price,size,title,outcome "
            f"FROM trades WHERE {where} ORDER BY timestamp,event_id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def ingestion_runs(self, wallet: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_id,started_at,finished_at,received,inserted,invalid,status,error "
            "FROM ingestion_runs WHERE wallet=? ORDER BY started_at DESC LIMIT ?",
            (self._normalize_wallet(wallet), limit),
        ).fetchall()
        return [dict(row) for row in rows]
