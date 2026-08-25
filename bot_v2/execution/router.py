"""Signal-to-execution glue layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from config.schema import AppConfig
from execution.order_builder import OrderBuilder
from execution.submitter import OrderSubmitter
from execution.tracker import OrderTracker
from models.events import BotEvent, EventType
from models.market import MarketSnapshot
from models.order import CancelIntent, CancelResult, OrderRequest, OrderResult
from models.risk import RiskAction
from models.signal import TradeSignal
from strategies.quoting import QuotePlan
from notifications.events import EventBus
from persistence.journal import JsonlJournal
from persistence.snapshots import SnapshotStore
from portfolio.sizing import fixed_size
from risk.pretrade import PreTradeRiskEngine
from state.reconciliation import ReconciliationReport
from state.store import InMemoryStateStore


@dataclass(frozen=True)
class RoutedOrder:
    """What a routed signal actually produced, for callers that track quotes."""

    request: OrderRequest
    result: OrderResult


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
        post_fill_reconcile: Callable[[], Awaitable[ReconciliationReport]] | None = None,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._risk_engine = risk_engine
        self._order_builder = order_builder
        self._submitter = submitter
        self._tracker = tracker
        self._journal = journal
        self._event_bus = event_bus
        self._post_fill_reconcile = post_fill_reconcile
        self._snapshots = snapshots

    async def route_signal(
        self,
        signal: TradeSignal,
        *,
        snapshot: MarketSnapshot | None = None,
        market_end_at: datetime | None = None,
    ) -> RoutedOrder | None:
        """
        Process one strategy signal through the full safe pipeline.

        Returns what was actually sent when an order reached the exchange, so
        a quoting strategy can record the resting order it now owns. Returns
        ``None`` whenever the signal was rejected before submission.
        """

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

        # A caller-supplied snapshot is only usable when it describes the book
        # this signal actually trades. Complement-routed signals are raised
        # from one outcome token but execute against the other, and risking
        # the wrong book silently defeats every price and liquidity check.
        if snapshot is not None and (
            snapshot.token_id != signal.token_id
            or snapshot.market_id != signal.market_id
        ):
            snapshot = None
        current_snapshot = snapshot or await self._state_store.get_market_snapshot(
            signal.market_id, signal.token_id
        )
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
                await self._release_exit_reservation(signal)
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
                return None
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
                activated = await self._activate_kill_switch(
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
            return None

        if current_snapshot is None or order_request is None:
            return None
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
            activated = await self._activate_kill_switch(
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
            activated = await self._activate_kill_switch(
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
            if self._post_fill_reconcile is not None:
                await self._reconcile_after_fill(result)
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
        return RoutedOrder(request=order_request, result=result)

    async def route_cancel(self, intent: CancelIntent) -> CancelResult:
        """Cancel one resting order and journal the outcome."""

        result = await self._submitter.cancel_order(intent)
        await self._emit_event(
            BotEvent(
                event_type=(
                    EventType.QUOTE_CANCELLED
                    if result.terminal
                    else EventType.QUOTE_CANCEL_FAILED
                ),
                component="router",
                mode=self._config.bot.mode.value,
                message=(
                    "resting quote cancelled"
                    if result.terminal
                    else "resting quote cancellation did not confirm"
                ),
                market_id=intent.market_id,
                token_id=intent.token_id,
                client_order_id=intent.client_order_id,
                reason=f"{intent.reason}:{result.outcome.value}",
            )
        )
        return result

    async def route_quote_plan(
        self,
        plan: QuotePlan,
        *,
        strategy: object,
        snapshot: MarketSnapshot | None = None,
        market_end_at: datetime | None = None,
    ) -> None:
        """
        Apply one quoting decision: cancel stale orders, then post new ones.

        Cancels run first and to completion. A cancellation that does not
        confirm leaves the old order live, so the replacement for that side is
        withheld and the quote is restored to the strategy's book -- posting
        anyway would double the intended exposure on that side.
        """

        forget = getattr(strategy, "forget_quote", None)
        blocked: set[tuple[str, str, str]] = set()
        for intent in plan.cancels:
            result = await self.route_cancel(intent)
            if result.terminal:
                if callable(forget):
                    forget(intent.client_order_id)
                continue
            # The order may still be live. Leave it tracked and withhold the
            # replacement for that side.
            blocked.add((intent.market_id, intent.token_id, intent.side.value))

        for quote_signal in plan.quotes:
            key = (
                quote_signal.market_id,
                quote_signal.token_id,
                quote_signal.side.value,
            )
            if key in blocked:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.RISK_DECISION,
                        component="quote_router",
                        mode=self._config.bot.mode.value,
                        message="quote withheld while a stale order is still live",
                        market_id=quote_signal.market_id,
                        token_id=quote_signal.token_id,
                        strategy_name=quote_signal.strategy_name,
                        signal_id=quote_signal.signal_id,
                        reason="replacement_blocked_by_unconfirmed_cancel",
                    )
                )
                continue
            routed = await self.route_signal(
                quote_signal,
                snapshot=snapshot,
                market_end_at=market_end_at,
            )
            if routed is None or not routed.result.accepted:
                continue
            register = getattr(strategy, "register_submission", None)
            if callable(register):
                register(
                    client_order_id=routed.request.client_order_id,
                    signal=quote_signal,
                    price=routed.request.price,
                    size=routed.request.size,
                )
            record = getattr(strategy, "record_order_result", None)
            if callable(record):
                record(routed.result)
            await self._emit_event(
                BotEvent(
                    event_type=EventType.QUOTE_PLACED,
                    component="quote_router",
                    mode=self._config.bot.mode.value,
                    message="resting quote placed",
                    market_id=routed.request.market_id,
                    token_id=routed.request.token_id,
                    strategy_name=routed.request.strategy_name,
                    signal_id=routed.request.signal_id,
                    client_order_id=routed.request.client_order_id,
                    quantity=routed.request.size,
                    price=routed.request.price,
                    reason=quote_signal.reason,
                )
            )

    async def _reconcile_after_fill(self, result: object) -> None:
        if self._post_fill_reconcile is None:
            return
        try:
            report = await self._post_fill_reconcile()
        except Exception as exc:
            activated = await self._activate_kill_switch(
                f"post_fill_reconciliation_failed:{type(exc).__name__}"
            )
            if activated:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="router",
                        mode=self._config.bot.mode.value,
                        message="post-fill reconciliation failed",
                        reason=f"post_fill_reconciliation_failed:{type(exc).__name__}",
                    )
                )
            return
        if not report.ok:
            activated = await self._activate_kill_switch(
                "post_fill_reconciliation_failed"
            )
            if activated:
                await self._emit_event(
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="router",
                        mode=self._config.bot.mode.value,
                        message="post-fill reconciliation failed",
                        reason="post_fill_reconciliation_failed",
                    )
                )
            return
        if report.deferred_positions:
            await self._emit_event(
                BotEvent(
                    event_type=EventType.POSITION_CONFIRMATION_DEFERRED,
                    component="router",
                    mode=self._config.bot.mode.value,
                    message="position confirmation deferred during grace period",
                    reason=",".join(report.deferred_positions),
                )
            )

    async def _release_exit_reservation(self, signal: TradeSignal) -> None:
        if not signal.reduce_only:
            return
        client_order_id = (
            f"{self._config.execution.client_order_id_prefix}-{signal.signal_id[:18]}"
        )
        released = await self._state_store.release_exit(
            signal.market_id,
            signal.token_id,
            client_order_id=client_order_id,
        )
        if released and self._snapshots is not None:
            await self._snapshots.save_from_state(self._state_store)

    async def _activate_kill_switch(self, reason: str) -> bool:
        activated = await self._state_store.activate_kill_switch(reason)
        if activated and self._snapshots is not None:
            await self._snapshots.save_from_state(self._state_store)
        return activated

    async def _emit_event(self, event: BotEvent) -> None:
        await self._journal.append(event)
        await self._event_bus.publish(event)
