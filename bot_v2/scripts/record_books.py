"""
Record live order-book snapshots for offline strategy research.

This exists because the mean-reversion thesis -- that a sharp move in a
short-duration crypto market overshoots and comes back -- has never been
measured on real data. Tuning thresholds before measuring the effect is
guesswork.

Writes newline-delimited JSON, one top-of-book observation per line, which
``scripts.analyze_reversion`` consumes. Read-only: it places no orders and
needs no credentials.

    python3 -m scripts.record_books --minutes 60 --output data/research/books.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from datetime import UTC, datetime

from pathlib import Path

from clients.gamma_markets import GammaMarketDiscoveryClient
from clients.market_data_client import MarketDataClient
from clients.ws_client import WebSocketManager
from config.loader import load_config
from config.schema import AppConfig
from models.market import MarketSnapshot
from state.store import InMemoryStateStore

logger = logging.getLogger("record_books")


def _serialize(snapshot: MarketSnapshot, *, slug: str, tick_size: str) -> str:
    return json.dumps(
        {
            "slug": slug,
            "market_id": snapshot.market_id,
            "token_id": snapshot.token_id,
            "tick_size": tick_size,
            "best_bid": str(snapshot.best_bid),
            "best_ask": str(snapshot.best_ask),
            "mid_price": str(snapshot.mid_price),
            "top_bid_size": str(snapshot.top_bid_size),
            "top_ask_size": str(snapshot.top_ask_size),
            "source_ts": snapshot.source_ts.isoformat(),
            "received_ts": snapshot.received_ts.isoformat(),
        }
    )


class BookRecorder:
    """Appends every observed top-of-book to a JSONL file."""

    def __init__(self, output: Path, *, slug: str, tick_size: str) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self._handle = output.open("a", encoding="utf-8")
        self._slug = slug
        self._tick_size = tick_size
        self.count = 0

    async def __call__(self, snapshot: MarketSnapshot) -> None:
        self._handle.write(
            _serialize(snapshot, slug=self._slug, tick_size=self._tick_size) + "\n"
        )
        self.count += 1
        if self.count % 5000 == 0:
            self._handle.flush()
            logger.info("recorded %s observations", self.count)

    def close(self) -> None:
        self._handle.flush()
        self._handle.close()


async def record(
    *,
    config: AppConfig,
    output: Path,
    minutes: float,
) -> int:
    """Discover the current market, stream its books, and record them."""

    gamma = GammaMarketDiscoveryClient(config.market_data.automatic_market)
    try:
        market = await gamma.discover_active()
    finally:
        await gamma.close()
    logger.info(
        "recording %s (%s) tokens=%s",
        market.slug,
        market.condition_id,
        market.asset_ids,
    )

    recorder = BookRecorder(output, slug=market.slug, tick_size="unknown")
    state_store = InMemoryStateStore(mode=config.bot.mode)
    market_data = MarketDataClient(
        state_store=state_store,
        on_snapshot=recorder,
    )
    ws = WebSocketManager(
        url=config.market_data.ws_url,
        asset_ids=market.asset_ids,
        on_message=market_data.handle_ws_message,
        reconnect_initial_seconds=config.market_data.reconnect_initial_seconds,
        reconnect_max_seconds=config.market_data.reconnect_max_seconds,
        ping_interval_seconds=config.exchange.ws_ping_interval_seconds,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await ws.start()
    try:
        await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
    except TimeoutError:
        pass
    finally:
        await ws.stop()
        recorder.close()

    logger.info("wrote %s observations to %s", recorder.count, output)
    return recorder.count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument(
        "--minutes", type=float, default=60.0, help="how long to record"
    )
    parser.add_argument(
        "--output",
        default="data/research/books.jsonl",
        help="JSONL destination; appended to, never truncated",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = load_config(args.config_dir)
    count = asyncio.run(
        record(config=config, output=Path(args.output), minutes=args.minutes)
    )
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
