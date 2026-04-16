"""Runtime risk checks executed while the bot is running."""

from __future__ import annotations

from decimal import Decimal

from config.schema import AppConfig
from models.risk import RiskAction, RiskCheckResult, RiskDecision
from risk.circuit_breaker import CircuitBreaker
from risk.policy import RuntimeRiskPolicy
from state.store import InMemoryStateStore


class RuntimeRiskEngine(RuntimeRiskPolicy):
    """Periodic runtime guardrail checks."""

    def __init__(
        self,
        *,
        config: AppConfig,
        state_store: InMemoryStateStore,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._circuit_breaker = circuit_breaker

    async def evaluate_runtime(self) -> RiskDecision:
        checks = [
            await self._daily_loss_check(),
            await self._heartbeat_check(),
            self._repeated_failure_check(),
        ]
        failed = [check for check in checks if not check.passed]
        if failed:
            return RiskDecision(
                action=RiskAction.HALT,
                approved=False,
                checks=checks,
                reason=failed[0].reason,
            )
        return RiskDecision(
            action=RiskAction.APPROVE,
            approved=True,
            checks=checks,
            reason="runtime_checks_passed",
        )

    async def _daily_loss_check(self) -> RiskCheckResult:
        positions = await self._state_store.get_positions()
        pnl = sum((position.realized_pnl + position.unrealized_pnl for position in positions), start=Decimal("0"))
        allowed_loss = self._config.risk.max_daily_loss
        return RiskCheckResult(
            check_name="daily_loss",
            passed=pnl >= -allowed_loss,
            reason="daily_loss_within_limit" if pnl >= -allowed_loss else f"daily_loss_limit:{pnl}<-{allowed_loss}",
        )

    async def _heartbeat_check(self) -> RiskCheckResult:
        stale = await self._state_store.is_heartbeat_stale(
            "market_data",
            max_age_seconds=self._config.market_data.heartbeat_timeout_seconds,
        )
        return RiskCheckResult(
            check_name="stale_heartbeat",
            passed=not stale,
            reason="heartbeat_fresh" if not stale else "heartbeat_stale",
        )

    def _repeated_failure_check(self) -> RiskCheckResult:
        state = self._circuit_breaker.state()
        return RiskCheckResult(
            check_name="repeated_failures",
            passed=not state.tripped,
            reason="failure_rate_normal" if not state.tripped else "circuit_breaker_tripped",
        )
