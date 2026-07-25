from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    name: str
    description: str
    amount: str
    fiat_currency: str


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    request_key: str
    telegram_chat_id: int
    telegram_user_id: int
    product_id: str
    product_name: str
    amount: str
    fiat_currency: str
    status: str
    makepay_status: str | None
    makepay_payment_uid: str | None
    checkout_url: str | None
    makepay_session_id: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaymentLink:
    uid: str
    checkout_url: str


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    status: str
    session_id: str | None


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    order: Order | None
    changed: bool
    duplicate: bool


@dataclass(frozen=True, slots=True)
class Notification:
    id: int
    order_id: str
    telegram_chat_id: int
    product_name: str
    amount: str
    fiat_currency: str
    status: str
    checkout_url: str | None
