"""CLI entry points for read-only ingestion, reporting, and local paper simulation."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

from .data_api import DataApiError, PolymarketDataClient, base_url_from_env
from .numeric import parse_decimal, parse_int
from .paper import PaperEngine, PaperPortfolio, QuoteSnapshot, RiskLimits
from .store import ResearchStore

SYNC_BATCH_SIZE = 500
_WALLET_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _wallet_arg(value: str) -> str:
    value = value.strip()
    if not _WALLET_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("wallet must be a 0x-prefixed 40-hex-digit address")
    return value.lower()


def _decimal_arg(value: str) -> Decimal:
    try:
        return parse_decimal(value, "value")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a finite, in-range number") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polybot", description="Read-only Polymarket research tools")
    sub = parser.add_subparsers(dest="command", required=True)
    trades = sub.add_parser("export-trades", help="export public wallet trades to JSONL")
    trades.add_argument("--wallet", required=True, type=_wallet_arg)
    trades.add_argument("--start", type=int, required=True, help="start Unix epoch seconds")
    trades.add_argument("--end", type=int, required=True, help="end Unix epoch seconds")
    trades.add_argument("--limit", type=int, default=100)
    trades.add_argument("--output", type=Path, required=True)

    sync = sub.add_parser("sync-trades", help="ingest normalized public trades into SQLite")
    sync.add_argument("--wallet", required=True, type=_wallet_arg)
    sync.add_argument("--start", type=int, required=True)
    sync.add_argument("--end", type=int, required=True)
    sync.add_argument("--limit", type=int, default=100)
    sync.add_argument("--db", type=Path, default=Path("data/research.sqlite3"))

    report = sub.add_parser("report", help="report observed trade activity; not P&L")
    report.add_argument("--wallet", required=True, type=_wallet_arg)
    report.add_argument("--start", type=int)
    report.add_argument("--end", type=int)
    report.add_argument("--db", type=Path, default=Path("data/research.sqlite3"))

    paper = sub.add_parser("paper-sim", help="simulate JSONL order intents against supplied quote snapshots")
    paper.add_argument("--input", type=Path, required=True, help="JSONL with order fields and nested quote object")
    paper.add_argument("--output", type=Path, required=True)
    paper.add_argument("--initial-cash", type=_decimal_arg, default=Decimal("100"))
    paper.add_argument("--max-order-size", type=_decimal_arg, default=Decimal("10"))
    paper.add_argument("--max-position-size", type=_decimal_arg, default=Decimal("25"))
    paper.add_argument("--max-notional", type=_decimal_arg, default=Decimal("10"))
    paper.add_argument("--max-quote-age", type=int, default=5)
    paper.add_argument("--fee-rate", type=_decimal_arg, default=Decimal("0"), help="explicit fee assumption, e.g. 0.02")
    paper.add_argument("--slippage-bps", type=_decimal_arg, default=Decimal("0"))
    paper.add_argument("--kill-switch", action="store_true")
    return parser


@contextlib.contextmanager
def _atomic_text_output(path: Path) -> Iterator[IO[str]]:
    """Write to a temp file beside ``path`` and rename on success.

    A failed or interrupted run therefore never leaves a truncated file that looks complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yield stream
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


async def _export(args: argparse.Namespace) -> int:
    count = 0
    try:
        async with PolymarketDataClient(base_url=base_url_from_env()) as client:
            with _atomic_text_output(args.output) as stream:
                async for row in client.iter_wallet_trades(args.wallet, start=args.start, end=args.end, limit=args.limit):
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
    except (DataApiError, OSError, ValueError) as exc:
        print(f"polybot: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} observed rows to {args.output}")
    return 0


async def _sync(args: argparse.Namespace) -> int:
    if args.start <= 0 or args.end < args.start:
        print("polybot: require 0 < start <= end", file=sys.stderr)
        return 2
    try:
        client = PolymarketDataClient(base_url=base_url_from_env())
    except ValueError as exc:
        print(f"polybot: {exc}", file=sys.stderr)
        return 2
    with ResearchStore(args.db) as store:
        run_id = store.start_run(args.wallet)
        try:
            async with client:
                batch: list[dict[str, Any]] = []
                async for row in client.iter_wallet_trades(args.wallet, start=args.start, end=args.end, limit=args.limit):
                    batch.append(row)
                    if len(batch) >= SYNC_BATCH_SIZE:
                        store.record_rows(run_id, batch)
                        batch.clear()
                if batch:
                    store.record_rows(run_id, batch)
            store.finish_run(run_id)
        except Exception as exc:
            # Any failure (including sqlite3.Error) must close out the run, or it stays 'running' forever.
            store.finish_run(run_id, error=f"{type(exc).__name__}: {exc}")
            print(f"polybot: ingestion failed; run_id={run_id}: {exc}", file=sys.stderr)
            return 1
        except BaseException:  # KeyboardInterrupt / task cancellation
            store.finish_run(run_id, error="interrupted")
            raise
        print(json.dumps(store.run_summary(run_id), sort_keys=True))
    return 0


def _report(args: argparse.Namespace) -> int:
    if args.start is not None and args.start <= 0:
        print("polybot: start must be a positive Unix timestamp", file=sys.stderr)
        return 2
    if args.end is not None and args.end <= 0:
        print("polybot: end must be a positive Unix timestamp", file=sys.stderr)
        return 2
    if args.start is not None and args.end is not None and args.start > args.end:
        print("polybot: start must be <= end", file=sys.stderr)
        return 2
    if not args.db.exists():
        # Opening would silently create an empty database (and its parent directory) on a typo.
        print(f"polybot: database not found: {args.db}", file=sys.stderr)
        return 1
    with ResearchStore(args.db) as store:
        result = store.report(args.wallet, start=args.start, end=args.end)
        result["recent_ingestion_runs"] = store.ingestion_runs(args.wallet)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _paper_sim(args: argparse.Namespace) -> int:
    try:
        limits = RiskLimits(
            max_order_size=args.max_order_size,
            max_position_size=args.max_position_size,
            max_notional=args.max_notional,
            max_quote_age_seconds=args.max_quote_age,
            fee_rate=args.fee_rate,
            slippage_bps=args.slippage_bps,
            kill_switch=args.kill_switch,
        )
        engine = PaperEngine(PaperPortfolio(args.initial_cash), limits)
        count = filled = rejected = 0
        with args.input.open("r", encoding="utf-8") as source, _atomic_text_output(args.output) as target:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    row: dict[str, Any] = json.loads(line)
                    quote = QuoteSnapshot.from_dict(row["quote"])
                    now = parse_int(row.get("now", row["timestamp"]), "now")
                    result = engine.execute(row, quote, now=now)
                except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                    # ArithmeticError covers decimal.* and OverflowError; json.JSONDecodeError is a ValueError.
                    result = {"status": "rejected", "reason": f"invalid input at line {line_number}: {exc}"}
                target.write(json.dumps(result, sort_keys=True) + "\n")
                count += 1
                filled += result.get("status") == "filled"
                rejected += result.get("status") == "rejected"
    except (OSError, ValueError) as exc:
        print(f"polybot: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"orders": count, "filled": filled, "rejected": rejected, "output": str(args.output)}))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "export-trades":
        code = asyncio.run(_export(args))
    elif args.command == "sync-trades":
        code = asyncio.run(_sync(args))
    elif args.command == "report":
        code = _report(args)
    elif args.command == "paper-sim":
        code = _paper_sim(args)
    else:
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
