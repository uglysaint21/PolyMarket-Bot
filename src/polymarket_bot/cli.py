"""CLI entry points for read-only ingestion, reporting, and local paper simulation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .data_api import DataApiError, PolymarketDataClient
from .paper import PaperEngine, PaperPortfolio, QuoteSnapshot, RiskLimits
from .store import ResearchStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polybot", description="Read-only Polymarket research tools")
    sub = parser.add_subparsers(dest="command", required=True)
    trades = sub.add_parser("export-trades", help="export public wallet trades to JSONL")
    trades.add_argument("--wallet", required=True)
    trades.add_argument("--start", type=int, required=True, help="start Unix epoch seconds")
    trades.add_argument("--end", type=int, required=True, help="end Unix epoch seconds")
    trades.add_argument("--limit", type=int, default=100)
    trades.add_argument("--output", type=Path, required=True)

    sync = sub.add_parser("sync-trades", help="ingest normalized public trades into SQLite")
    sync.add_argument("--wallet", required=True)
    sync.add_argument("--start", type=int, required=True)
    sync.add_argument("--end", type=int, required=True)
    sync.add_argument("--limit", type=int, default=100)
    sync.add_argument("--db", type=Path, default=Path("data/research.sqlite3"))

    report = sub.add_parser("report", help="report observed trade activity; not P&L")
    report.add_argument("--wallet", required=True)
    report.add_argument("--start", type=int)
    report.add_argument("--end", type=int)
    report.add_argument("--db", type=Path, default=Path("data/research.sqlite3"))

    paper = sub.add_parser("paper-sim", help="simulate JSONL order intents against supplied quote snapshots")
    paper.add_argument("--input", type=Path, required=True, help="JSONL with order fields and nested quote object")
    paper.add_argument("--output", type=Path, required=True)
    paper.add_argument("--initial-cash", type=Decimal, default=Decimal("100"))
    paper.add_argument("--max-order-size", type=Decimal, default=Decimal("10"))
    paper.add_argument("--max-position-size", type=Decimal, default=Decimal("25"))
    paper.add_argument("--max-notional", type=Decimal, default=Decimal("10"))
    paper.add_argument("--max-quote-age", type=int, default=5)
    paper.add_argument("--fee-rate", type=Decimal, default=Decimal("0"), help="explicit fee assumption, e.g. 0.02")
    paper.add_argument("--slippage-bps", type=Decimal, default=Decimal("0"))
    paper.add_argument("--kill-switch", action="store_true")
    return parser


async def _client_context():
    client = PolymarketDataClient()
    try:
        yield client
    finally:
        await client.aclose()


async def _export(args: argparse.Namespace) -> int:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        async with _AsyncClientContext() as client:
            with args.output.open("w", encoding="utf-8") as stream:
                async for row in client.iter_wallet_trades(args.wallet, start=args.start, end=args.end, limit=args.limit):
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
    except (DataApiError, OSError, ValueError) as exc:
        print(f"polybot: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} observed rows to {args.output}")
    return 0


class _AsyncClientContext:
    async def __aenter__(self) -> PolymarketDataClient:
        self.client = PolymarketDataClient()
        return self.client

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.client.aclose()


async def _sync(args: argparse.Namespace) -> int:
    if args.start <= 0 or args.end < args.start:
        print("polybot: require 0 < start <= end", file=sys.stderr)
        return 2
    store = ResearchStore(args.db)
    run_id = store.start_run(args.wallet)
    try:
        async with _AsyncClientContext() as client:
            async for row in client.iter_wallet_trades(args.wallet, start=args.start, end=args.end, limit=args.limit):
                store.record_row(run_id, row)
        store.finish_run(run_id)
    except (DataApiError, OSError, ValueError, RuntimeError) as exc:
        store.finish_run(run_id, error=str(exc))
        print(f"polybot: ingestion failed; run_id={run_id}: {exc}", file=sys.stderr)
        return 1
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
    store = ResearchStore(args.db)
    result = store.report(args.wallet, start=args.start, end=args.end)
    result["recent_ingestion_runs"] = store.ingestion_runs(args.wallet)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _paper_sim(args: argparse.Namespace) -> int:
    args.output.parent.mkdir(parents=True, exist_ok=True)
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
        with args.input.open("r", encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    row: dict[str, Any] = json.loads(line)
                    quote = QuoteSnapshot.from_dict(row["quote"])
                    now = int(row.get("now", row["timestamp"]))
                    result = engine.execute(row, quote, now=now)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
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


def main() -> None:
    args = _parser().parse_args()
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
