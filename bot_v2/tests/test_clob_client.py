from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from clients.auth import ClobCredentials
from clients.clob_client import (
    ClobAdapterError,
    ClobClientAdapter,
    ClobUncertainOutcomeError,
)
from config.schema import AppConfig, Mode
from models.order import OrderRequest, OrderSide, OrderStatus, OrderTimeInForce


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )


def eoa_live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
        exchange={"signature_type": 0},
    )


def complete_credentials() -> ClobCredentials:
    return ClobCredentials(
        private_key="private-key",
        proxy_address="0x1111111111111111111111111111111111111111",
        api_key="api-key",
        secret="api-secret",
        passphrase="passphrase",
        rpc_url="https://rpc.example",
    )


def buy_request(*, size: str = "1", price: str = "0.50") -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal(price),
        size=Decimal(size),
        time_in_force=OrderTimeInForce.GTC,
        strategy_name="test",
    )


class FakeV2Client:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[object] = []

    def get_ok(self) -> str:
        self.calls.append("get_ok")
        return "OK"

    def get_open_orders(self, params: object = None, only_first_page: bool = False, next_cursor: object = None) -> list[dict[str, object]]:
        self.calls.append(("get_open_orders", params, only_first_page, next_cursor))
        return []

    def get_order(self, order_id: str) -> dict[str, object]:
        self.calls.append(("get_order", order_id))
        return {
            "id": order_id,
            "status": "ORDER_STATUS_MATCHED",
            "market": "m1",
            "asset_id": "t1",
            "side": "BUY",
            "original_size": "1000000",
            "size_matched": "1000000",
            "price": "0.5",
        }

    def get_balance_allowance(self, params: object = None) -> dict[str, object]:
        self.calls.append(("get_balance_allowance", params))
        return {
            "balance": "9760000",
            "allowances": {
                "exchange": "1000000",
                "neg-risk-exchange": "2000000",
                "neg-risk-adapter": "500000",
            },
        }

    def create_order(self, order_args: object) -> dict[str, object]:
        self.calls.append(("create_order", order_args))
        return {"signed": True}

    def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
        self.calls.append(("post_order", order, order_type))
        return {
            "success": True,
            "orderID": "0xabc123",
            "status": "live",
            "makingAmount": "500000",
            "takingAmount": "1000000",
            "errorMsg": "",
        }

    def cancel_order(self, payload: object) -> dict[str, object]:
        self.calls.append(("cancel_order", payload))
        return {"canceled": [payload.orderID], "not_canceled": {}}

    def cancel_all(self) -> dict[str, object]:
        self.calls.append("cancel_all")
        return {"canceled": ["0xorder0001"], "not_canceled": {}}


def exploding_factory(**kwargs: object) -> object:
    raise AssertionError("SDK must not be constructed")


def test_v2_factory_passes_l1_l2_signature_and_funder() -> None:
    from py_clob_client_v2 import ApiCreds

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    assert adapter._client.kwargs == {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": "private-key",
        "creds": ApiCreds(
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="passphrase",
        ),
        "signature_type": 3,
        "funder": "0x1111111111111111111111111111111111111111",
    }


def test_v2_factory_derives_eoa_funder_from_private_key() -> None:
    from py_clob_client_v2 import ApiCreds

    credentials = complete_credentials()
    credentials = ClobCredentials(
        private_key="0x" + "1" * 64,
        proxy_address=None,
        api_key=credentials.api_key,
        secret=credentials.secret,
        passphrase=credentials.passphrase,
        rpc_url=credentials.rpc_url,
    )

    adapter = ClobClientAdapter.from_v2(
        config=eoa_live_config(),
        credentials=credentials,
        sdk_factory=FakeV2Client,
    )

    assert adapter._client.kwargs == {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": "0x" + "1" * 64,
        "creds": ApiCreds(
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="passphrase",
        ),
        "signature_type": 0,
        "funder": "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
    }


def test_read_only_v2_adapter_cannot_submit_or_cancel() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
        read_only=True,
    )

    with pytest.raises(ClobAdapterError, match="submission disabled"):
        adapter.submit_order(buy_request())
    with pytest.raises(ClobAdapterError, match="cancellation disabled"):
        adapter.cancel_all()


def test_v2_factory_rejects_missing_private_key_before_sdk_construction() -> None:
    credentials = complete_credentials()
    credentials = ClobCredentials(
        private_key=None,
        proxy_address=credentials.proxy_address,
        api_key=credentials.api_key,
        secret=credentials.secret,
        passphrase=credentials.passphrase,
        rpc_url=credentials.rpc_url,
    )
    with pytest.raises(ClobAdapterError, match="private key"):
        ClobClientAdapter.from_v2(
            config=live_config(),
            credentials=credentials,
            sdk_factory=exploding_factory,
        )


def test_v2_factory_rejects_incomplete_l2_credentials_before_sdk_construction() -> None:
    credentials = complete_credentials()
    credentials = ClobCredentials(
        private_key=credentials.private_key,
        proxy_address=credentials.proxy_address,
        api_key="api-key",
        secret=None,
        passphrase="passphrase",
        rpc_url=credentials.rpc_url,
    )
    with pytest.raises(ClobAdapterError, match="L2"):
        ClobClientAdapter.from_v2(
            config=live_config(),
            credentials=credentials,
            sdk_factory=exploding_factory,
        )


def test_v2_factory_rejects_missing_funder_before_sdk_construction() -> None:
    credentials = complete_credentials()
    credentials = ClobCredentials(
        private_key=credentials.private_key,
        proxy_address=None,
        api_key=credentials.api_key,
        secret=credentials.secret,
        passphrase=credentials.passphrase,
        rpc_url=credentials.rpc_url,
    )
    with pytest.raises(ClobAdapterError, match="funder"):
        ClobClientAdapter.from_v2(
            config=live_config(),
            credentials=credentials,
            sdk_factory=exploding_factory,
        )


def test_healthcheck_returns_true_for_ok_response() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    assert adapter.healthcheck() is True
    assert adapter._client.calls == ["get_ok"]


def test_healthcheck_fails_closed_on_unexpected_response() -> None:
    class BadOkClient(FakeV2Client):
        def get_ok(self) -> str:
            return "DOWN"

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=BadOkClient,
    )
    with pytest.raises(ClobAdapterError, match="health"):
        adapter.healthcheck()


def test_get_open_orders_normalizes_v2_rows() -> None:
    class OrdersClient(FakeV2Client):
        def get_open_orders(self, params: object = None, only_first_page: bool = False, next_cursor: object = None) -> list[dict[str, object]]:
            self.calls.append(("get_open_orders", params, only_first_page, next_cursor))
            return [
                {
                    "id": "0xorder0001",
                    "market": "m1",
                    "asset_id": "t1",
                    "side": "SELL",
                    "price": "0.45",
                    "original_size": "5000000",
                    "size_matched": "1000000",
                }
            ]

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=OrdersClient,
    )
    orders = adapter.get_open_orders()
    assert len(orders) == 1
    assert orders[0].client_order_id == "0xorder0001"
    assert orders[0].exchange_order_id == "0xorder0001"
    assert orders[0].requested_size == Decimal("5")
    assert orders[0].filled_size == Decimal("1")
    assert orders[0].status == OrderStatus.PARTIALLY_FILLED
    assert orders[0].side == OrderSide.SELL
    assert orders[0].avg_fill_price == Decimal("0.45")


def test_get_open_orders_fails_closed_on_malformed_response() -> None:
    class MalformedOrdersClient(FakeV2Client):
        def get_open_orders(self, params: object = None, only_first_page: bool = False, next_cursor: object = None) -> object:
            return {"data": "not-a-list"}

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=MalformedOrdersClient,
    )
    with pytest.raises(ClobAdapterError, match="open orders"):
        adapter.get_open_orders()


def test_get_order_normalizes_terminal_fill_and_preserves_client_id() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )

    result = adapter.get_order(
        "0xorder0001",
        client_order_id="client-order-0001",
    )

    assert result.client_order_id == "client-order-0001"
    assert result.exchange_order_id == "0xorder0001"
    assert result.status == OrderStatus.FILLED
    assert result.filled_size == Decimal("1")
    assert result.avg_fill_price == Decimal("0.5")


def test_get_order_normalizes_exchange_cancellation() -> None:
    class CancelledOrderClient(FakeV2Client):
        def get_order(self, order_id: str) -> dict[str, object]:
            row = super().get_order(order_id)
            row["status"] = "ORDER_STATUS_CANCELED_MARKET_RESOLVED"
            row["size_matched"] = "0"
            return row

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=CancelledOrderClient,
    )

    result = adapter.get_order("0xorder0001")

    assert result.status == OrderStatus.CANCELLED


def test_get_collateral_status_normalizes_balance_and_allowance() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    status = adapter.get_collateral_status()
    assert status.balance == Decimal("9.76")
    assert status.allowance == Decimal("0.5")
    call = adapter._client.calls[0]
    assert call[0] == "get_balance_allowance"
    assert call[1].asset_type == "COLLATERAL"


def test_get_collateral_status_fails_closed_on_malformed_response() -> None:
    class BadAllowanceClient(FakeV2Client):
        def get_balance_allowance(self, params: object = None) -> object:
            return "not-a-dict"

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=BadAllowanceClient,
    )
    with pytest.raises(ClobAdapterError, match="collateral"):
        adapter.get_collateral_status()


def test_submit_order_rejects_notional_above_live_cap_before_signing() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    with pytest.raises(ClobAdapterError, match="notional cap"):
        adapter.submit_order(buy_request(size="5", price="0.50"))
    assert adapter._client.calls == []


def test_submit_order_maps_side_and_time_in_force_and_normalizes_response() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    result = adapter.submit_order(buy_request(size="1", price="0.50"))
    assert result.status == OrderStatus.SUBMITTED
    assert result.accepted is True
    assert result.exchange_order_id == "0xabc123"
    create_call = adapter._client.calls[0]
    assert create_call[0] == "create_order"
    assert create_call[1].token_id == "t1"
    assert create_call[1].price == 0.5
    assert create_call[1].size == 1.0
    from py_clob_client_v2 import Side

    assert create_call[1].side == Side.BUY
    post_call = adapter._client.calls[1]
    assert post_call[0] == "post_order"
    assert post_call[2] == "GTC"


def test_submit_order_treats_local_order_creation_failure_as_definite() -> None:
    class SigningFailureClient(FakeV2Client):
        def create_order(self, order_args: object) -> dict[str, object]:
            raise ValueError("invalid signing input")

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=SigningFailureClient,
    )

    with pytest.raises(ClobAdapterError, match="order creation failed") as exc_info:
        adapter.submit_order(buy_request(size="1", price="0.50"))

    assert not isinstance(exc_info.value, ClobUncertainOutcomeError)


def test_submit_order_treats_post_transport_failure_as_uncertain() -> None:
    class PostTimeoutClient(FakeV2Client):
        def post_order(
            self,
            order: object,
            order_type: object = "GTC",
            post_only: bool = False,
            defer_exec: bool = False,
        ) -> dict[str, object]:
            raise TimeoutError("request timed out")

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=PostTimeoutClient,
    )

    with pytest.raises(ClobUncertainOutcomeError, match="outcome unknown"):
        adapter.submit_order(buy_request(size="1", price="0.50"))


def test_submit_order_treats_exchange_http_rejection_as_definite() -> None:
    from py_clob_client_v2.exceptions import PolyApiException

    class RejectedPostClient(FakeV2Client):
        def post_order(
            self,
            order: object,
            order_type: object = "GTC",
            post_only: bool = False,
            defer_exec: bool = False,
        ) -> dict[str, object]:
            raise PolyApiException(
                resp=httpx.Response(400, json={"error": "rejected"})
            )

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=RejectedPostClient,
    )

    with pytest.raises(ClobAdapterError, match="rejected:http_400") as exc_info:
        adapter.submit_order(buy_request(size="1", price="0.50"))

    assert not isinstance(exc_info.value, ClobUncertainOutcomeError)


def test_submit_order_requires_exchange_order_id_for_acceptance() -> None:
    class NoOrderIdClient(FakeV2Client):
        def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
            return {"success": True}

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=NoOrderIdClient,
    )
    result = adapter.submit_order(buy_request(size="1", price="0.50"))
    assert result.accepted is False
    assert result.status == OrderStatus.REJECTED


def test_submit_order_rejects_explicit_unsuccessful_response_even_with_order_id() -> None:
    class RejectedClient(FakeV2Client):
        def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
            return {
                "success": False,
                "orderID": "0xrejected",
                "status": "live",
                "errorMsg": "insufficient balance",
            }

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=RejectedClient,
    )
    result = adapter.submit_order(buy_request(size="1", price="0.50"))
    assert result.accepted is False
    assert result.status == OrderStatus.REJECTED
    assert result.message == "insufficient balance"


def test_submit_order_normalizes_partial_matched_amounts_from_fixed_six() -> None:
    class PartiallyMatchedClient(FakeV2Client):
        def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
            return {
                "success": True,
                "orderID": "0xpartial",
                "status": "matched",
                "makingAmount": "250000",
                "takingAmount": "500000",
                "errorMsg": "",
            }

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=PartiallyMatchedClient,
    )
    result = adapter.submit_order(buy_request(size="1", price="0.50"))
    assert result.accepted is True
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.filled_size == Decimal("0.5")
    assert result.avg_fill_price == Decimal("0.5")


def test_fok_submission_requires_immediate_matched_status() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    order = buy_request().model_copy(update={"time_in_force": OrderTimeInForce.FOK})

    result = adapter.submit_order(order)

    assert result.accepted is False
    assert result.status == OrderStatus.UNKNOWN
    assert result.message == "fok_fill_not_confirmed:live"


def test_fok_submission_rejects_partial_matched_response() -> None:
    class PartiallyMatchedFokClient(FakeV2Client):
        def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
            return {
                "success": True,
                "orderID": "0xpartial",
                "status": "matched",
                "makingAmount": "250000",
                "takingAmount": "500000",
                "errorMsg": "",
            }

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=PartiallyMatchedFokClient,
    )
    order = buy_request().model_copy(update={"time_in_force": OrderTimeInForce.FOK})

    result = adapter.submit_order(order)

    assert result.accepted is False
    assert result.status == OrderStatus.UNKNOWN
    assert result.filled_size == Decimal("0.5")
    assert result.message == "fok_partial_fill_invariant_violation"


def test_submit_order_fails_closed_on_malformed_response() -> None:
    class MalformedPostClient(FakeV2Client):
        def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> object:
            return "not-a-dict"

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=MalformedPostClient,
    )
    with pytest.raises(ClobAdapterError, match="submission"):
        adapter.submit_order(buy_request(size="1", price="0.50"))


def test_cancel_order_and_cancel_all_use_explicit_v2_methods() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    assert adapter.cancel_order("0xorder0001") is True
    assert adapter._client.calls[0][0] == "cancel_order"
    assert adapter._client.calls[0][1].orderID == "0xorder0001"
    assert adapter.cancel_all() is True
    assert adapter._client.calls[1] == "cancel_all"


def test_cancel_order_fails_when_exchange_does_not_confirm_requested_id() -> None:
    class FailedCancelClient(FakeV2Client):
        def cancel_order(self, payload: object) -> dict[str, object]:
            return {
                "canceled": [],
                "not_canceled": {payload.orderID: "order already matched"},
            }

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FailedCancelClient,
    )
    with pytest.raises(ClobAdapterError, match="not canceled"):
        adapter.cancel_order("0xorder0001")


def test_cancel_all_fails_on_partial_exchange_failure() -> None:
    class PartialCancelAllClient(FakeV2Client):
        def cancel_all(self) -> dict[str, object]:
            return {
                "canceled": ["0xorder0001"],
                "not_canceled": {"0xorder0002": "order already matched"},
            }

    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=PartialCancelAllClient,
    )
    with pytest.raises(ClobAdapterError, match="not canceled"):
        adapter.cancel_all()


def test_disabled_adapter_blocks_submission_and_cancellation() -> None:
    adapter = ClobClientAdapter.disabled()
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.submit_order(buy_request())
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.cancel_order("0xorder0001")
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.cancel_all()
    assert adapter.get_open_orders() == []
