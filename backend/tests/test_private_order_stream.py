import base64
import hashlib
import hmac
from decimal import Decimal

from cosmos_quant.private_order_stream import (
    okx_login_payload,
    parse_okx_order_event,
)


def test_okx_login_signature_matches_documented_preimage():
    payload = okx_login_payload("key", "secret", "pass", "1538054050")
    expected = base64.b64encode(
        hmac.new(
            b"secret",
            b"1538054050GET/users/self/verify",
            hashlib.sha256,
        ).digest()
    ).decode()

    assert payload["op"] == "login"
    assert payload["args"][0]["sign"] == expected


def test_okx_private_order_event_normalizes_contract_quantity_and_fee():
    event = parse_okx_order_event(
        {
            "ordId": "123",
            "clOrdId": "cx_123",
            "instId": "BTC-USDT-SWAP",
            "state": "partially_filled",
            "accFillSz": "12",
            "avgPx": "60000.5",
            "fee": "-0.42",
            "uTime": "1783921204000",
        },
        lambda _instrument, quantity: quantity * Decimal("0.01"),
    )

    assert event is not None
    assert event.status == "partially_filled"
    assert event.filled_quantity == Decimal("0.12")
    assert event.average_fill_price == Decimal("60000.5")
    assert event.fee == Decimal("0.42")
