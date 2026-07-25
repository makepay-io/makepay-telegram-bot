import asyncio
import json

import httpx
import pytest

from makepay_telegram_bot.makepay_client import MakePayAPIError, MakePayClient
from makepay_telegram_bot.models import Order, Product

ORDER = Order(
    id="05d0ab75-fc70-4058-b876-d9bb386734c7",
    request_key="callback:1",
    telegram_chat_id=100,
    telegram_user_id=200,
    product_id="coffee",
    product_name="Coffee voucher",
    amount="5.00",
    fiat_currency="USD",
    status="creating",
    makepay_status=None,
    makepay_payment_uid=None,
    checkout_url=None,
    makepay_session_id=None,
    last_error=None,
    created_at="2026-07-25T00:00:00Z",
    updated_at="2026-07-25T00:00:00Z",
)
PRODUCT = Product(
    id="coffee",
    name="Coffee voucher",
    description="A demo product",
    amount="5.00",
    fiat_currency="USD",
)


def test_creates_payment_link_with_auth_and_idempotency() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "paymentLink": {
                    "uid": "pay_123",
                    "publicUrl": "https://www.makepay.io/payment/pay_123",
                }
            },
        )

    async def run() -> None:
        client = MakePayClient(
            key_id="key-id",
            key_secret="key-secret",
            api_base_url="https://www.makecrypto.io",
            checkout_base_url="https://www.makepay.io",
            transport=httpx.MockTransport(handler),
        )
        link = await client.create_payment_link(ORDER, PRODUCT)
        await client.close()
        assert link.uid == "pay_123"

    asyncio.run(run())
    request = requests[0]
    body = json.loads(request.content)
    assert request.headers["idempotency-key"] == f"telegram-{ORDER.id}"
    assert request.headers["x-makecrypto-key-id"] == "key-id"
    assert body["payload"]["orderId"] == ORDER.id
    assert body["payload"]["amount"] == "5.00"
    assert "telegram_chat_id" not in request.content.decode()


def test_public_status_request_does_not_leak_api_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"session": {"id": "session_1", "status": "complete"}})

    async def run() -> None:
        client = MakePayClient(
            key_id="secret-key-id",
            key_secret="secret-key-value",
            api_base_url="https://www.makecrypto.io",
            checkout_base_url="https://www.makepay.io",
            transport=httpx.MockTransport(handler),
        )
        status = await client.get_payment_status("pay_123")
        await client.close()
        assert status.status == "complete"

    asyncio.run(run())
    assert requests[0].url.host == "www.makepay.io"
    assert "x-makecrypto-key-id" not in requests[0].headers
    assert "x-makecrypto-key-secret" not in requests[0].headers


def test_falls_back_to_canonical_checkout_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"paymentLink": {"uid": "pay/unsafe"}})

    async def run() -> None:
        client = MakePayClient(
            key_id="id",
            key_secret="secret",
            api_base_url="https://api.example.com",
            checkout_base_url="https://pay.example.com",
            transport=httpx.MockTransport(handler),
        )
        link = await client.create_payment_link(ORDER, PRODUCT)
        await client.close()
        assert link.checkout_url == "https://pay.example.com/payment/pay%2Funsafe"

    asyncio.run(run())


def test_raises_sanitized_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API credentials."})

    async def run() -> None:
        client = MakePayClient(
            key_id="id",
            key_secret="secret",
            api_base_url="https://api.example.com",
            checkout_base_url="https://pay.example.com",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(MakePayAPIError, match="Invalid API credentials") as caught:
            await client.create_payment_link(ORDER, PRODUCT)
        assert caught.value.status_code == 401
        await client.close()

    asyncio.run(run())


def test_rejects_missing_uid_unsafe_url_and_invalid_json() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"paymentLink": {}}),
            httpx.Response(
                200,
                json={
                    "paymentLink": {
                        "uid": "pay_123",
                        "publicUrl": "javascript:alert(1)",
                    }
                },
            ),
            httpx.Response(200, content=b"not-json"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async def run() -> None:
        client = MakePayClient(
            key_id="id",
            key_secret="secret",
            api_base_url="https://api.example.com",
            checkout_base_url="https://pay.example.com",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(MakePayAPIError, match="UID"):
            await client.create_payment_link(ORDER, PRODUCT)
        with pytest.raises(MakePayAPIError, match="unsafe"):
            await client.create_payment_link(ORDER, PRODUCT)
        with pytest.raises(MakePayAPIError, match="invalid JSON"):
            await client.get_payment_status("pay_123")
        await client.close()

    asyncio.run(run())


def test_missing_current_session_is_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": None})

    async def run() -> None:
        client = MakePayClient(
            key_id="id",
            key_secret="secret",
            api_base_url="https://api.example.com",
            checkout_base_url="https://pay.example.com",
            transport=httpx.MockTransport(handler),
        )
        status = await client.get_payment_status("pay_123")
        assert status.status == "pending"
        await client.close()

    asyncio.run(run())
