"""Runtime mode helpers."""

from __future__ import annotations

from config.schema import Mode


def supports_execution(mode: Mode) -> bool:
    """Whether the mode can run the signal-to-order pipeline."""

    return mode in {Mode.DRY_RUN, Mode.LIVE, Mode.BACKTEST, Mode.REPLAY}


def is_live_mode(mode: Mode) -> bool:
    """Whether the mode is live trading."""

    return mode == Mode.LIVE


def is_dry_run_mode(mode: Mode) -> bool:
    """Whether the mode is dry-run."""

    return mode == Mode.DRY_RUN
