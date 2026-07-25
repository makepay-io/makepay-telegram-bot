import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from telegram import Bot

from makepay_telegram_bot.app import Runtime, create_app
from makepay_telegram_bot.config import Settings
from makepay_telegram_bot.database import Database
from makepay_telegram_bot.models import Product

PRODUCT = Product(
    id="coffee",
    name="Coffee voucher",
    description="A demo product",
    amount="5.00",
    fiat_currency="USD",
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:ABCDEF",
        telegram_webhook_secret="telegram_secret",
        public_base_url="https://bot.example.com",
        makepay_key_id="key-id",
        makepay_key_secret="key-secret",
        makepay_webhook_secret="makepay-secret",
        makepay_api_base_url="https://www.makecrypto.io",
        makepay_checkout_base_url="https://www.makepay.io",
        database_path=tmp_path / "orders.db",
        catalog_path=tmp_path / "catalog.json",
        admin_telegram_user_ids=frozenset(),
        reconcile_interval_seconds=60,
        notification_interval_seconds=10,
        max_webhook_body_bytes=4096,
        log_level="INFO",
    )


def signature(body: bytes, secret: str) -> str:
    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_http_endpoints_authenticate_and_apply_webhooks(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_app()
        configuration = settings(tmp_path)
        database = Database(configuration.database_path)
        order = database.create_or_get_order(
            request_key="callback:1",
            telegram_chat_id=100,
            telegram_user_id=200,
            product=PRODUCT,
        )
        database.attach_payment_link(order.id, "pay_123", "https://www.makepay.io/payment/pay_123")
        telegram = SimpleNamespace(
            bot=Bot(configuration.telegram_bot_token),
            update_queue=asyncio.Queue(),
            running=True,
        )
        app.state.runtime = Runtime(
            settings=configuration,
            database=database,
            makepay=SimpleNamespace(),  # type: ignore[arg-type]
            telegram=telegram,  # type: ignore[arg-type]
            notification_wakeup=asyncio.Event(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://bot.example.com"
        ) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.get("/readyz")).status_code == 200

            invalid_telegram = await client.post(
                "/webhooks/telegram",
                headers={"x-telegram-bot-api-secret-token": "wrong"},
                json={"update_id": 1},
            )
            assert invalid_telegram.status_code == 401

            valid_telegram = await client.post(
                "/webhooks/telegram",
                headers={"x-telegram-bot-api-secret-token": configuration.telegram_webhook_secret},
                json={"update_id": 1},
            )
            assert valid_telegram.status_code == 200
            assert telegram.update_queue.qsize() == 1

            payload = {
                "deliveryId": "delivery_1",
                "paymentLink": {
                    "uid": "pay_123",
                    "merchantOrderId": order.id,
                },
                "session": {"id": "session_1", "status": "complete"},
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            invalid_makepay = await client.post(
                "/webhooks/makepay",
                content=body,
                headers={"x-makepay-signature": "t=1,v1=bad"},
            )
            assert invalid_makepay.status_code == 401

            valid_makepay = await client.post(
                "/webhooks/makepay",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-makepay-signature": signature(body, configuration.makepay_webhook_secret),
                    "x-makepay-delivery-group-id": "group_1",
                },
            )
            assert valid_makepay.json() == {
                "ok": True,
                "matched": True,
                "duplicate": False,
            }
            updated = database.get_order_for_owner(order.id, 100, 200)
            assert updated is not None and updated.status == "paid"

            duplicate = await client.post(
                "/webhooks/makepay",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-makepay-signature": signature(body, configuration.makepay_webhook_secret),
                    "x-makepay-delivery-group-id": "group_1",
                },
            )
            assert duplicate.json()["duplicate"] is True
        database.close()

    asyncio.run(run())


def test_webhook_size_and_json_validation(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_app()
        configuration = settings(tmp_path)
        database = Database(configuration.database_path)
        telegram = SimpleNamespace(
            bot=Bot(configuration.telegram_bot_token),
            update_queue=asyncio.Queue(),
            running=True,
        )
        app.state.runtime = Runtime(
            settings=configuration,
            database=database,
            makepay=SimpleNamespace(),  # type: ignore[arg-type]
            telegram=telegram,  # type: ignore[arg-type]
            notification_wakeup=asyncio.Event(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://bot.example.com",
        ) as client:
            headers = {"x-telegram-bot-api-secret-token": configuration.telegram_webhook_secret}
            invalid = await client.post(
                "/webhooks/telegram",
                headers={**headers, "content-type": "application/json"},
                content=b"{",
            )
            assert invalid.status_code == 400
            oversized = await client.post(
                "/webhooks/telegram",
                headers={**headers, "content-length": "5000"},
                content=b"{}",
            )
            assert oversized.status_code == 413
        database.close()

    asyncio.run(run())


def test_readiness_is_unavailable_before_runtime_initializes() -> None:
    async def run() -> None:
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://bot.example.com",
        ) as client:
            response = await client.get("/readyz")
            assert response.status_code == 503

    asyncio.run(run())
