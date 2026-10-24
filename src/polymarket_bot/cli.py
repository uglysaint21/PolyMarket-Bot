"""CLI entry point for bounded, read-only wallet trade export."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .data_api import DataApiError, PolymarketDataClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polybot", description="Read-only Polymarket research tools")
    sub = parser.add_subparsers(dest="command", required=True)
    trades = sub.add_parser("export-trades", help="export public wallet trades to JSONL")
    trades.add_argument("--wallet", required=True, help="public wallet/proxy address to research")
    trades.add_argument("--start", type=int, required=True, help="start Unix epoch seconds")
    trades.add_argument("--end", type=int, required=True, help="end Unix epoch seconds")
    trades.add_argument("--limit", type=int, default=100, help="page size (1-1000)")
    trades.add_argument("--output", type=Path, required=True, help="output JSONL path")
    return parser


async def _export(args: argparse.Namespace) -> int:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        async with _client_context() as client:
            with args.output.open("w", encoding="utf-8") as stream:
                async for row in client.iter_wallet_trades(
                    args.wallet, start=args.start, end=args.end, limit=args.limit
                ):
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
    except (DataApiError, OSError, ValueError) as exc:
        print(f"polybot: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} observed rows to {args.output}")
    return 0


class _client_context:
    async def __aenter__(self) -> PolymarketDataClient:
        self.client = PolymarketDataClient()
        return self.client

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.client.aclose()


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_export(args)))


if __name__ == "__main__":
    main()
