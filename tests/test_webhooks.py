import hashlib
import hmac
import json
import time

import pytest
from makepay import MakePayError, parse_makepay_webhook

from makepay_telegram_bot.webhooks import extract_makepay_event


def sign(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    issued_at = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(
        secret.encode(), str(issued_at).encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return f"t={issued_at},v1={signature}"


def test_verifies_and_extracts_makepay_webhook() -> None:
    payload = {
        "deliveryId": "delivery_1",
        "type": "makepay.payment.status_changed",
        "paymentLink": {"uid": "pay_123", "merchantOrderId": "order_123"},
        "session": {"id": "session_123", "status": "complete"},
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    parsed = parse_makepay_webhook(raw_body, sign(raw_body, "whsec"), "whsec")

    event = extract_makepay_event(
        parsed,
        {
            "x-makepay-delivery-group-id": "group_1",
            "x-makepay-event": "makepay.payment.status_changed",
        },
        raw_body,
    )

    assert event.dedupe_key == "group_1"
    assert event.payment_uid == "pay_123"
    assert event.merchant_order_id == "order_123"
    assert event.status == "complete"


def test_rejects_invalid_or_stale_signature() -> None:
    body = b'{"ok":true}'
    with pytest.raises(MakePayError):
        parse_makepay_webhook(body, sign(body, "wrong"), "correct")
    with pytest.raises(MakePayError):
        parse_makepay_webhook(body, sign(body, "secret", 1), "secret")


def test_falls_back_to_stable_body_hash_without_delivery_headers() -> None:
    body = b'{"status":"pending"}'
    event = extract_makepay_event({"status": "pending"}, {}, body)

    assert event.dedupe_key == f"body:{hashlib.sha256(body).hexdigest()}"
    assert event.status == "pending"
