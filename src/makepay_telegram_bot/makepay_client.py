from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .models import Order, PaymentLink, PaymentStatus, Product


class MakePayAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _record(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class MakePayClient:
    """Minimal async MakePay API client for this integration."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        api_base_url: str,
        checkout_base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._checkout_base_url = checkout_base_url.rstrip("/")
        self._api_client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            follow_redirects=False,
            timeout=httpx.Timeout(20),
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "MakePayTelegramBot/1.0.0",
                "X-MakeCrypto-Key-Id": key_id,
                "X-MakeCrypto-Key-Secret": key_secret,
            },
        )
        self._public_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(20),
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "MakePayTelegramBot/1.0.0",
            },
        )

    async def close(self) -> None:
        await self._api_client.aclose()
        await self._public_client.aclose()

    async def create_payment_link(self, order: Order, product: Product) -> PaymentLink:
        response = await self._api_client.post(
            "/api/partner/v1/makepay/payment-links",
            headers={"Idempotency-Key": f"telegram-{order.id}"},
            json={
                "status": "active",
                "sendPaymentRequestEmail": False,
                "payload": {
                    "title": product.name,
                    "description": product.description,
                    "amount": product.amount,
                    "fiatCurrency": product.fiat_currency,
                    "orderId": order.id,
                    "expirationTime": "1h",
                    "metadata": {
                        "integration": "telegram-bot",
                        "telegramOrderId": order.id,
                    },
                },
            },
        )
        payload = self._decode(response, "create payment link")
        payment_link = _record(payload.get("paymentLink"))
        uid = _text(payment_link.get("uid"))
        if not uid:
            raise MakePayAPIError("MakePay response did not include a payment-link UID.")
        checkout_url = _text(payment_link.get("publicUrl")) or _text(
            payment_link.get("checkoutUrl")
        )
        if not checkout_url:
            checkout_url = f"{self._checkout_base_url}/payment/{quote(uid, safe='')}"
        if not checkout_url.startswith("https://") and not checkout_url.startswith(
            "http://localhost"
        ):
            raise MakePayAPIError("MakePay response included an unsafe checkout URL.")
        return PaymentLink(uid=uid, checkout_url=checkout_url)

    async def get_payment_status(self, payment_uid: str) -> PaymentStatus:
        response = await self._public_client.get(
            f"{self._checkout_base_url}/api/public/payment-links/"
            f"{quote(payment_uid, safe='')}/current-session"
        )
        payload = self._decode(response, "read payment status")
        session = _record(payload.get("session"))
        status = _text(session.get("status"))
        if not status:
            return PaymentStatus(status="pending", session_id=None)
        return PaymentStatus(status=status, session_id=_text(session.get("id")))

    @staticmethod
    def _decode(response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            decoded: object = response.json()
        except ValueError as error:
            raise MakePayAPIError(
                f"MakePay returned invalid JSON while trying to {action}.",
                status_code=response.status_code,
            ) from error
        body = _record(decoded)
        if not response.is_success:
            message = _text(body.get("error")) or f"MakePay could not {action}."
            raise MakePayAPIError(message, status_code=response.status_code)
        return body
