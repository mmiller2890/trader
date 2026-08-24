"""Pure fault policy mapping incidents to recovery actions."""

from __future__ import annotations

from dataclasses import dataclass

from config.schema import ReliabilityConfig
from models.operations import IncidentCategory, OperationalIncident


class RecoveryAction:
    RETRY = "retry"
    DEGRADE = "degrade"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    flat: bool
    authoritative_unavailable_seconds: float = 0
    open_position_exit_window_seconds: float | None = None
    repeated_authoritative_confirmations: int = 0
    task_crashes_in_window: int = 0
    disk_percent: float = 0
    required_for_safe_exit: bool = False


class FaultPolicy:
    """Pure decision object mapping an incident and context to a recovery action."""

    def __init__(self, config: ReliabilityConfig) -> None:
        self._config = config

    def decide(
        self, incident: OperationalIncident, context: RecoveryContext
    ) -> str:
        category = incident.category

        if category in (
            IncidentCategory.AUTHENTICATION,
            IncidentCategory.COMPLIANCE,
            IncidentCategory.ACCOUNTING,
        ):
            return RecoveryAction.HALT

        if category == IncidentCategory.EXIT_SAFETY:
            if context.flat:
                return RecoveryAction.DEGRADE
            return RecoveryAction.HALT

        if category == IncidentCategory.TASK_CRASH:
            if context.task_crashes_in_window >= self._config.task_restart_limit + 1:
                return RecoveryAction.HALT
            return RecoveryAction.RETRY

        if category == IncidentCategory.DISK:
            if context.disk_percent >= self._config.disk_halt_percent:
                return RecoveryAction.HALT
            if context.disk_percent >= self._config.disk_degraded_percent:
                return RecoveryAction.DEGRADE
            return RecoveryAction.RETRY

        if category == IncidentCategory.ACCOUNT_DIVERGENCE:
            if context.repeated_authoritative_confirmations >= 2:
                return RecoveryAction.HALT
            return RecoveryAction.DEGRADE

        if category == IncidentCategory.FUNDING:
            if context.required_for_safe_exit:
                return RecoveryAction.HALT
            return RecoveryAction.DEGRADE

        if category == IncidentCategory.AUTHORITATIVE_STATE:
            exposed = not context.flat
            if (
                exposed
                and context.authoritative_unavailable_seconds
                >= self._config.authoritative_state_halt_after_seconds
            ):
                return RecoveryAction.HALT
            return RecoveryAction.DEGRADE

        if category == IncidentCategory.MARKET_DISCOVERY:
            if not context.flat:
                return RecoveryAction.DEGRADE
            return RecoveryAction.RETRY

        return RecoveryAction.RETRY
