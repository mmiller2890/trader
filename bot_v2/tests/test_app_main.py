from __future__ import annotations

from app.main import parser


def test_cli_requires_explicit_live_flag() -> None:
    assert parser().parse_args([]).live is False
    assert parser().parse_args(["--live"]).live is True
