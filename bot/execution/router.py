"""Signal-to-execution glue layer."""

from __future__ import annotations

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
        risk_decision = await self._risk_engine.evaluate(
            signal=signal,
            snapshot=current_snapshot,
            proposed_size=proposed_size,
            proposed_price=proposed_price,
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
                await self._state_store.set_kill_switch(True)
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
            return

        if current_snapshot is None:
            return

        order_request = self._order_builder.build(
            signal=signal,
            snapshot=current_snapshot,
            size=proposed_size,
        )
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
        await self._tracker.handle_order_result(result)

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

    async def _emit_event(self, event: BotEvent) -> None:
        await self._journal.append(event)
        await self._event_bus.publish(event)
