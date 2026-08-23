from __future__ import annotations

from decimal import Decimal

import pytest

from clients.auth import ClobCredentials
from clients.clob_client import ClobAdapterError, ClobClientAdapter
from config.schema import AppConfig, Mode
from models.order import OrderRequest, OrderSide, OrderStatus, OrderTimeInForce


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
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

    def get_balance_allowance(self, params: object = None) -> dict[str, str]:
        self.calls.append(("get_balance_allowance", params))
        return {"balance": "100.5", "allowance": "1000"}

    def create_order(self, order_args: object) -> dict[str, object]:
        self.calls.append(("create_order", order_args))
        return {"signed": True}

    def post_order(self, order: object, order_type: object = "GTC", post_only: bool = False, defer_exec: bool = False) -> dict[str, object]:
        self.calls.append(("post_order", order, order_type))
        return {"success": True, "orderID": "0xabc123"}

    def cancel_order(self, payload: object) -> dict[str, object]:
        self.calls.append(("cancel_order", payload))
        return {"success": True}

    def cancel_all(self) -> dict[str, object]:
        self.calls.append("cancel_all")
        return {"success": True}


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
                    "original_size": "5",
                    "size_matched": "1",
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
    assert orders[0].status == OrderStatus.SUBMITTED


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


def test_get_collateral_status_normalizes_balance_and_allowance() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    status = adapter.get_collateral_status()
    assert status.balance == Decimal("100.5")
    assert status.allowance == Decimal("1000")
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


def test_disabled_adapter_blocks_submission_and_cancellation() -> None:
    adapter = ClobClientAdapter.disabled()
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.submit_order(buy_request())
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.cancel_order("0xorder0001")
    with pytest.raises(ClobAdapterError, match="disabled"):
        adapter.cancel_all()
    assert adapter.get_open_orders() == []
