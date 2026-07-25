from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any


def _record(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object, *, maximum: int = 300) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]


@dataclass(frozen=True, slots=True)
class MakePayWebhookEvent:
    dedupe_key: str
    delivery_id: str | None
    event_type: str | None
    payment_uid: str | None
    merchant_order_id: str | None
    session_id: str | None
    status: str | None


def extract_makepay_event(
    payload: dict[str, Any], headers: dict[str, str], raw_body: bytes
) -> MakePayWebhookEvent:
    payment_link = _record(payload.get("paymentLink"))
    session = _record(payload.get("session"))
    event = _record(payload.get("event"))
    delivery_id = _text(payload.get("deliveryId"))
    delivery_group_id = _text(headers.get("x-makepay-delivery-group-id"))
    event_type = (
        _text(headers.get("x-makepay-event"))
        or _text(payload.get("type"))
        or _text(event.get("type"))
    )
    body_hash = sha256(raw_body).hexdigest()
    return MakePayWebhookEvent(
        dedupe_key=delivery_group_id or delivery_id or f"body:{body_hash}",
        delivery_id=delivery_id,
        event_type=event_type,
        payment_uid=_text(payment_link.get("uid")),
        merchant_order_id=_text(payment_link.get("merchantOrderId")),
        session_id=_text(session.get("id")),
        status=_text(session.get("status")) or _text(payload.get("status")),
    )
