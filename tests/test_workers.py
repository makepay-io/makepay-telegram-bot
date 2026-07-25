import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from makepay_telegram_bot.database import Database
from makepay_telegram_bot.models import PaymentStatus, Product
from makepay_telegram_bot.workers import notification_loop, reconciliation_loop

PRODUCT = Product(
    id="coffee",
    name="Coffee voucher",
    description="A demo product",
    amount="5.00",
    fiat_currency="USD",
)


class FakeMakePay:
    async def get_payment_status(self, payment_uid: str) -> PaymentStatus:
        return PaymentStatus(status="complete", session_id="session_123")


def prepared_database(path: Path) -> tuple[Database, str]:
    database = Database(path)
    order = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )
    database.attach_payment_link(order.id, "pay_123", "https://www.makepay.io/payment/pay_123")
    return database, order.id


def test_reconciliation_and_notification_workers(tmp_path: Path) -> None:
    async def run() -> None:
        database, order_id = prepared_database(tmp_path / "orders.db")
        wakeup = asyncio.Event()
        reconcile = asyncio.create_task(
            reconciliation_loop(
                makepay=FakeMakePay(),  # type: ignore[arg-type]
                database=database,
                notification_wakeup=wakeup,
                interval_seconds=3600,
            )
        )
        for _ in range(20):
            order = database.get_order_for_owner(order_id, 100, 200)
            if order is not None and order.status == "paid":
                break
            await asyncio.sleep(0)
        reconcile.cancel()
        try:
            await reconcile
        except asyncio.CancelledError:
            pass
        assert database.pending_notifications()[0].status == "paid"

        bot = SimpleNamespace(send_message=AsyncMock())
        application = SimpleNamespace(bot=bot)
        notify = asyncio.create_task(
            notification_loop(
                application=application,  # type: ignore[arg-type]
                database=database,
                wakeup=wakeup,
                interval_seconds=3600,
            )
        )
        for _ in range(20):
            if not database.pending_notifications():
                break
            await asyncio.sleep(0)
        notify.cancel()
        try:
            await notify
        except asyncio.CancelledError:
            pass
        bot.send_message.assert_awaited_once()
        assert "Payment complete" in bot.send_message.await_args.args[1]
        database.close()

    asyncio.run(run())
