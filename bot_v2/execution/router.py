"""Signal-to-execution glue layer."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from config.schema import AppConfig
from execution.order_builder import OrderBuilder
from execution.submitter import OrderSubmitter
from execution.tracker import OrderTracker
from models.events import BotEvent, EventType
from models.market import MarketSnapshot
from models.risk import RiskAction
from models.signal import TradeSignal
from notifications.events import EventBus
from persistence.journal import JsonlJournal
from portfolio.sizing import fixed_size
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore


class ExecutionRouter:
    """Coordinates risk, order building, submission, and state updates."""

    def __init__(
        self,
        *,
        config: AppConfig,
        state_store: InMemoryStateStore,
        risk_engine: PreTradeRiskEngine,
        order_builder: OrderBuilder,
        submitter: OrderSubmitter,
        tracker: OrderTracker,
        journal: JsonlJournal,
        event_bus: EventBus,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._risk_engine = risk_engine
        self._order_builder = order_builder
        self._submitter = submitter
        self._tracker = tracker
        self._journal = journal
        self._event_bus = event_bus

    async def route_signal(
        self,
        signal: TradeSignal,
        *,
        snapshot: MarketSnapshot | None = None,
        market_end_at: datetime | None = None,
    ) -> None:
        """Process one strategy signal through the full safe pipeline."""

        await self._state_store.add_signal(signal)
        await self._emit_event(
            BotEvent(
                event_type=EventType.SIGNAL_GENERATED,
                component="router",
                mode=self._config.bot.mode.value,
                message="strategy signal received",
                market_id=signal.market_id,
                token_id=signal.token_id,
                strategy_name=signal.strategy_name,
                signal_id=signal.signal_id,
                reason=signal.reason,
            )
        )

        current_snapshot = snapshot or await self._state_store.get_market_snapshot(signal.market_id, signal.token_id)
        proposed_size = fixed_size(self._config.execution)
        proposed_price = (
            current_snapshot.best_ask if current_snapshot and signal.side.value == "buy"
            else current_snapshot.best_bid if current_snapshot
            else Decimal("0")
        )
        order_request = None
        executable_liquidity = None
        if current_snapshot is not None:
            try:
                order_request = self._order_builder.build(
                    signal=signal,
                    snapshot=current_snapshot,
                    size=proposed_size,
                )
            except ValueError as exc:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.RISK_DECISION,
                        component="execution_planner",
                        mode=self._config.bot.mode.value,
                        message="execution plan rejected",
                        market_id=signal.market_id,
                        token_id=signal.token_id,
                        strategy_name=signal.strategy_name,
                        signal_id=signal.signal_id,
                        reason=str(exc),
                    )
                )
                return
            proposed_size = order_request.size
            proposed_price = order_request.price
            executable_liquidity = (
                current_snapshot.top_ask_size
                if signal.side.value == "buy"
                else current_snapshot.top_bid_size
            )
        risk_decision = await self._risk_engine.evaluate(
            signal=signal,
            snapshot=current_snapshot,
            proposed_size=proposed_size,
            proposed_price=proposed_price,
            executable_liquidity=executable_liquidity,
        )
        await self._emit_event(
            BotEvent(
                event_type=EventType.RISK_DECISION,
                component="pretrade_risk",
                mode=self._config.bot.mode.value,
                message="risk decision emitted",
                market_id=signal.market_id,
                token_id=signal.token_id,
                strategy_name=signal.strategy_name,
                signal_id=signal.signal_id,
                reason=risk_decision.reason,
            )
        )

        if not risk_decision.approved:
            if risk_decision.action == RiskAction.HALT:
                activated = await self._state_store.activate_kill_switch(
                    risk_decision.reason
                )
                if activated:
                    await self._emit_event(
                        BotEvent(
                            event_type=EventType.KILL_SWITCH_TRIPPED,
                            component="router",
                            mode=self._config.bot.mode.value,
                            message="kill switch activated from risk halt",
                            market_id=signal.market_id,
                            token_id=signal.token_id,
                            strategy_name=signal.strategy_name,
                            signal_id=signal.signal_id,
                            reason=risk_decision.reason,
                        )
                    )
            else:
                await self._release_exit_reservation(signal)
            return

        if current_snapshot is None or order_request is None:
            return
        await self._emit_event(
            BotEvent(
                event_type=EventType.ORDER_SUBMITTED,
                component="router",
                mode=self._config.bot.mode.value,
                message="order ready for submission",
                market_id=order_request.market_id,
                token_id=order_request.token_id,
                strategy_name=order_request.strategy_name,
                signal_id=order_request.signal_id,
                client_order_id=order_request.client_order_id,
            )
        )
        result = await self._submitter.submit(order_request)
        outcome = await self._tracker.handle_order_result(
            result, market_end_at=market_end_at
        )

        if outcome.unknown_outcome:
            activated = await self._state_store.activate_kill_switch(
                f"unknown_order_outcome:{result.client_order_id}"
            )
            if activated:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="router",
                        mode=self._config.bot.mode.value,
                        message="unknown order outcome latched kill switch",
                        market_id=result.market_id,
                        token_id=result.token_id,
                        client_order_id=result.client_order_id,
                        reason=f"unknown_order_outcome:{result.client_order_id}",
                    )
                )
        elif outcome.accounting_error is not None:
            activated = await self._state_store.activate_kill_switch(
                f"position_accounting_error:{outcome.accounting_error}"
            )
            if activated:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="router",
                        mode=self._config.bot.mode.value,
                        message="position accounting error latched kill switch",
                        market_id=result.market_id,
                        token_id=result.token_id,
                        client_order_id=result.client_order_id,
                        reason=f"position_accounting_error:{outcome.accounting_error}",
                    )
                )
        elif outcome.fill_applied and outcome.fill_application is not None:
            application = outcome.fill_application
            if signal.reduce_only:
                await self._release_exit_reservation(signal)
            if outcome.position_closed:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.POSITION_CLOSED,
                        component="tracker",
                        mode=self._config.bot.mode.value,
                        message="position closed",
                        market_id=result.market_id,
                        token_id=result.token_id,
                        quantity=Decimal("0"),
                        price=result.avg_fill_price,
                        pnl=application.position.realized_pnl
                        if application.position is not None
                        else None,
                    )
                )
            else:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.POSITION_UPDATED,
                        component="tracker",
                        mode=self._config.bot.mode.value,
                        message="position updated from confirmed fill",
                        market_id=result.market_id,
                        token_id=result.token_id,
                        quantity=application.position.quantity
                        if application.position is not None
                        else None,
                        price=result.avg_fill_price,
                    )
                )
        elif signal.reduce_only and result.status.value in {"rejected", "failed"}:
            await self._release_exit_reservation(signal)

        large_reason = None
        if result.status.value == "simulated" and result.requested_size >= self._config.notifications.large_order_threshold:
            large_reason = "large_order_simulated"
        await self._emit_event(
            BotEvent(
                event_type=EventType.ORDER_RESULT,
                component="submitter",
                mode=self._config.bot.mode.value,
                message="order result received",
                market_id=result.market_id,
                token_id=result.token_id,
                strategy_name=result.strategy_name,
                signal_id=result.signal_id,
                client_order_id=result.client_order_id,
                reason=large_reason or result.message,
                latency_ms=result.latency_ms,
            )
        )

    async def _release_exit_reservation(self, signal: TradeSignal) -> None:
        if not signal.reduce_only:
            return
        client_order_id = (
            f"{self._config.execution.client_order_id_prefix}-{signal.signal_id[:18]}"
        )
        await self._state_store.release_exit(
            signal.market_id,
            signal.token_id,
            client_order_id=client_order_id,
        )

    async def _emit_event(self, event: BotEvent) -> None:
        await self._journal.append(event)
        await self._event_bus.publish(event)
