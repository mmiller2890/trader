"""Cost-aware edge gating."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from config.schema import AppConfig, Mode
from risk.edge import EdgeDecision, assess_edge


def assess(**kwargs: object):
    base: dict[str, object] = {
        "edge_bps": Decimal("120"),
        "price": Decimal("0.50"),
        "spread_bps": Decimal("200"),
        "fee_rate": Decimal("0.07"),
        "is_maker_entry": True,
        "safety_margin_bps": Decimal("50"),
    }
    base.update(kwargs)
    return assess_edge(**base)  # type: ignore[arg-type]


def test_a_maker_entry_still_pays_a_modelled_taker_exit() -> None:
    result = assess()

    # -100 (half spread earned) +350 (exit fee) +100 (exit half spread) +50
    assert result.required_bps == Decimal("400")
    assert result.decision is EdgeDecision.ABSTAIN


def test_a_taker_entry_costs_more_than_a_maker_entry() -> None:
    maker = assess(is_maker_entry=True)
    taker = assess(is_maker_entry=False)

    assert taker.required_bps > maker.required_bps
    # Taker adds its own fee plus the half spread it pays to cross.
    assert taker.required_bps - maker.required_bps == Decimal("550")


def test_a_genuinely_profitable_signal_is_approved() -> None:
    result = assess(edge_bps=Decimal("900"))
    assert result.decision is EdgeDecision.APPROVE


def test_the_assessment_carries_the_numbers_that_decided_it() -> None:
    result = assess()

    assert result.fee_bps == Decimal("350")
    assert result.spread_bps == Decimal("200")
    assert result.edge_bps == Decimal("120")
    assert "required" in result.reason


def test_cheap_extremes_require_less_edge_than_the_midpoint() -> None:
    mid = assess(price=Decimal("0.50"))
    high = assess(price=Decimal("0.90"))

    assert high.required_bps < mid.required_bps


def test_shadow_mode_is_refused_in_live_mode() -> None:
    with pytest.raises(ValidationError, match="shadow"):
        AppConfig(
            bot={"mode": Mode.LIVE},
            execution={"allow_live_trading": True, "dry_run_force": False},
            risk={"edge_gate_mode": "shadow"},
        )


def test_shadow_mode_is_allowed_in_dry_run() -> None:
    config = AppConfig(bot={"mode": Mode.DRY_RUN}, risk={"edge_gate_mode": "shadow"})
    assert config.risk.edge_gate_mode == "shadow"


def test_enforce_mode_is_the_default() -> None:
    assert AppConfig().risk.edge_gate_mode == "enforce"
