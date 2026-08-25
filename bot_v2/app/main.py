"""Application runtime entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from app.process_services import build_process_reliability_services
from app.runtime import BotRuntime, FatalRuntimeError
from config.loader import load_config
from config.schema import Mode
from persistence.snapshots import SnapshotStore
from reliability.lease import LiveResumeRejected


def parser() -> argparse.ArgumentParser:
    """Build the CLI parser; live startup must be explicitly requested."""

    result = argparse.ArgumentParser(description="Run the Polymarket bot")
    result.add_argument(
        "--live",
        action="store_true",
        help="allow a fully armed live configuration to start",
    )
    result.add_argument(
        "--resume-live",
        action="store_true",
        help="resume live trading only through a persisted valid lease",
    )
    return result


async def run(*, allow_live: bool = False, resume_live: bool = False) -> None:
    """Boot and run the main bot runtime."""

    config = load_config(None)
    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    process_services = build_process_reliability_services(
        config=config,
        data_dir=data_dir,
    )
    runtime = BotRuntime(process_services=process_services)
    loop = asyncio.get_running_loop()

    try:
        await process_services.start()
        effective_allow_live = allow_live
        if resume_live:
            if config.bot.mode != Mode.LIVE:
                raise FatalRuntimeError("resume_requires_live_mode")
            snapshot = await SnapshotStore(
                data_dir / "snapshots" / "state.json"
            ).load()
            if snapshot is not None and snapshot.kill_switch_active:
                raise FatalRuntimeError("kill_switch_latched")
            try:
                await process_services.leases.validate_for_resume(
                    config, now=datetime.now(tz=UTC)
                )
            except LiveResumeRejected as exc:
                raise FatalRuntimeError(exc.reason) from exc
            effective_allow_live = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runtime.request_stop)

        status = await runtime.start(allow_live=effective_allow_live)
        if status.phase.value == "failed":
            await runtime.stop()
            raise FatalRuntimeError(status.reason or "bot_start_failed")
        status = await runtime.wait()
        if status.phase.value == "failed":
            raise FatalRuntimeError(status.reason or "bot_failed")
    finally:
        try:
            await runtime.stop()
        finally:
            await process_services.close()


def main(argv: list[str] | None = None) -> None:
    """Synchronous CLI entrypoint."""

    args = parser().parse_args(argv)
    if args.resume_live and args.live:
        raise SystemExit("--live and --resume-live are mutually exclusive")
    asyncio.run(run(allow_live=args.live, resume_live=args.resume_live))


if __name__ == "__main__":
    main()
