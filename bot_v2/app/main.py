"""Application runtime entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import signal
from app.runtime import BotRuntime, emit_event, housekeeping_loop


def parser() -> argparse.ArgumentParser:
    """Build the CLI parser; live startup must be explicitly requested."""

    result = argparse.ArgumentParser(description="Run the Polymarket bot")
    result.add_argument(
        "--live",
        action="store_true",
        help="allow a fully armed live configuration to start",
    )
    return result


async def run(*, allow_live: bool = False) -> None:
    """Boot and run the main bot runtime."""

    runtime = BotRuntime()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runtime.request_stop)

    try:
        status = await runtime.start(allow_live=allow_live)
        if status.phase.value == "failed":
            raise RuntimeError(status.reason or "bot_start_failed")
        await runtime.wait()
    finally:
        await runtime.stop()


def main(argv: list[str] | None = None) -> None:
    """Synchronous CLI entrypoint."""

    args = parser().parse_args(argv)
    asyncio.run(run(allow_live=args.live))


if __name__ == "__main__":
    main()
