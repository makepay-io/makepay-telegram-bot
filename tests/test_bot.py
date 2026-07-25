import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from makepay_telegram_bot.bot import BotHandlers
from makepay_telegram_bot.database import Database
from makepay_telegram_bot.models import PaymentLink, PaymentStatus, Product

PRODUCT = Product(
    id="coffee",
    name="Coffee voucher",
    description="A demo product",
    amount="5.00",
    fiat_currency="USD",
)


class FakeMakePay:
    async def create_payment_link(self, order: object, product: object) -> PaymentLink:
        return PaymentLink(uid="pay_123", checkout_url="https://www.makepay.io/payment/pay_123")

    async def get_payment_status(self, payment_uid: str) -> PaymentStatus:
        return PaymentStatus(status="complete", session_id="session_123")


def private_update(
    *,
    data: str | None = None,
    query_id: str = "query_1",
    chat_type: str = "private",
) -> SimpleNamespace:
    message = SimpleNamespace(reply_text=AsyncMock())
    query = (
        SimpleNamespace(
            id=query_id,
            data=data,
            answer=AsyncMock(),
        )
        if data is not None
        else None
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=200),
        effective_chat=SimpleNamespace(id=100, type=chat_type),
        effective_message=message,
    )


def handlers(tmp_path: Path) -> tuple[BotHandlers, Database]:
    database = Database(tmp_path / "orders.db")
    instance = BotHandlers(
        catalog={PRODUCT.id: PRODUCT},
        database=database,
        makepay=FakeMakePay(),  # type: ignore[arg-type]
    )
    return instance, database


def test_start_help_and_shop(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        start = private_update()
        await instance.start(start, None)  # type: ignore[arg-type]
        await instance.help(start, None)  # type: ignore[arg-type]
        await instance.shop(start, None)  # type: ignore[arg-type]
        assert start.effective_message.reply_text.await_count == 3
        shop_call = start.effective_message.reply_text.await_args_list[-1]
        assert shop_call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "buy:coffee"
        database.close()

    asyncio.run(run())


def test_group_purchase_is_refused(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        update = private_update(data="buy:coffee", chat_type="group")
        await instance.buy(update, None)  # type: ignore[arg-type]
        update.callback_query.answer.assert_awaited_once()
        assert database.list_orders(100, 200) == []
        database.close()

    asyncio.run(run())


def test_buy_is_idempotent_and_sends_checkout(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        first = private_update(data="buy:coffee")
        await instance.buy(first, None)  # type: ignore[arg-type]
        second = private_update(data="buy:coffee")
        await instance.buy(second, None)  # type: ignore[arg-type]

        orders = database.list_orders(100, 200)
        assert len(orders) == 1
        assert orders[0].makepay_payment_uid == "pay_123"
        assert (
            first.effective_message.reply_text.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
            .url
            == "https://www.makepay.io/payment/pay_123"
        )
        assert second.effective_message.reply_text.await_count == 1
        database.close()

    asyncio.run(run())


def test_unknown_product_and_empty_orders(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        update = private_update(data="buy:missing")
        await instance.buy(update, None)  # type: ignore[arg-type]
        assert "no longer available" in update.effective_message.reply_text.await_args.args[0]

        empty = private_update()
        await instance.orders(empty, None)  # type: ignore[arg-type]
        assert "do not have any orders" in empty.effective_message.reply_text.await_args.args[0]
        database.close()

    asyncio.run(run())


def test_status_refresh_marks_order_paid(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        buy = private_update(data="buy:coffee")
        await instance.buy(buy, None)  # type: ignore[arg-type]
        order = database.list_orders(100, 200)[0]

        status = private_update(data=f"status:{order.id}", query_id="query_2")
        await instance.status(status, None)  # type: ignore[arg-type]

        refreshed = database.get_order_for_owner(order.id, 100, 200)
        assert refreshed is not None and refreshed.status == "paid"
        assert "is paid" in status.callback_query.answer.await_args.args[0]
        database.close()

    asyncio.run(run())


def test_orders_render_and_private_chat_guard(tmp_path: Path) -> None:
    async def run() -> None:
        instance, database = handlers(tmp_path)
        buy = private_update(data="buy:coffee")
        await instance.buy(buy, None)  # type: ignore[arg-type]

        listing = private_update()
        await instance.orders(listing, None)  # type: ignore[arg-type]
        assert "Coffee voucher" in listing.effective_message.reply_text.await_args.args[0]

        group = private_update(chat_type="group")
        await instance.shop(group, None)  # type: ignore[arg-type]
        assert "private chat" in group.effective_message.reply_text.await_args.args[0]
        database.close()

    asyncio.run(run())
