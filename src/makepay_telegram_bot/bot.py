from __future__ import annotations

import html
import logging

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .database import Database
from .makepay_client import MakePayAPIError, MakePayClient
from .models import Order, Product
from .telegram_types import TelegramApplication

logger = logging.getLogger(__name__)


def _money(amount: str, currency: str) -> str:
    return f"{amount} {currency}"


def _status_label(status: str) -> str:
    return {
        "creating": "creating checkout",
        "creation_failed": "checkout creation failed",
        "pending": "awaiting payment",
        "processing": "processing",
        "underpaid": "underpaid",
        "paid": "paid",
        "expired": "expired",
        "cancelled": "cancelled",
        "failed": "failed",
    }.get(status, status)


class BotHandlers:
    def __init__(
        self,
        *,
        catalog: dict[str, Product],
        database: Database,
        makepay: MakePayClient,
    ) -> None:
        self._catalog = catalog
        self._database = database
        self._makepay = makepay

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_private_chat(update):
            return
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            "Welcome! This demo shop creates a secure MakePay checkout inside "
            "Telegram. Choose /shop to browse or /orders to check your payments."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            "/shop — browse products\n"
            "/orders — view your five latest orders\n"
            "/help — show this message\n\n"
            "Payments happen on MakePay's hosted checkout. The bot never asks "
            "for a seed phrase, private key, or card details."
        )

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_private_chat(update):
            return
        message = update.effective_message
        if message is None:
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{product.name} · {_money(product.amount, product.fiat_currency)}",
                    callback_data=f"buy:{product.id}",
                )
            ]
            for product in self._catalog.values()
        ]
        await message.reply_text(
            "Choose a product. You will review and pay on MakePay's hosted checkout.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_private_chat(update):
            return
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return
        orders = self._database.list_orders(chat.id, user.id)
        if not orders:
            await message.reply_text("You do not have any orders yet. Use /shop to start.")
            return
        lines = ["Your latest orders:"]
        for order in orders:
            lines.append(
                f"• {html.escape(order.product_name)} · "
                f"{_money(order.amount, order.fiat_currency)} · "
                f"{_status_label(order.status)} · #{order.id[:8]}"
            )
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        message = update.effective_message
        if query is None or user is None or chat is None or message is None:
            return
        if chat.type != ChatType.PRIVATE:
            await query.answer("Open the bot in a private chat to buy.", show_alert=True)
            return
        await query.answer("Creating a secure checkout…")
        product_id = (query.data or "").removeprefix("buy:")
        product = self._catalog.get(product_id)
        if product is None:
            await message.reply_text("That product is no longer available. Use /shop again.")
            return

        order = self._database.create_or_get_order(
            request_key=f"telegram-callback:{query.id}",
            telegram_chat_id=chat.id,
            telegram_user_id=user.id,
            product=product,
        )
        if order.checkout_url:
            await self._send_checkout(message, order)
            return

        try:
            link = await self._makepay.create_payment_link(order, product)
            order = self._database.attach_payment_link(order.id, link.uid, link.checkout_url)
        except MakePayAPIError as error:
            logger.warning(
                "MakePay checkout creation failed for order %s with status %s",
                order.id,
                error.status_code,
            )
            self._database.mark_creation_failed(
                order.id, f"makepay_http_{error.status_code or 'unknown'}"
            )
            await message.reply_text(
                f"We could not create checkout for order #{order.id[:8]}. "
                "Please tap the product again in /shop; the retry is safe."
            )
            return
        await self._send_checkout(message, order)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        message = update.effective_message
        if query is None or user is None or chat is None or message is None:
            return
        order_id = (query.data or "").removeprefix("status:")
        order = self._database.get_order_for_owner(order_id, chat.id, user.id)
        if order is None:
            await query.answer("Order not found.", show_alert=True)
            return
        if order.makepay_payment_uid and order.status not in {
            "paid",
            "cancelled",
            "expired",
            "failed",
        }:
            try:
                remote = await self._makepay.get_payment_status(order.makepay_payment_uid)
                result = self._database.apply_payment_status(
                    dedupe_key=(
                        f"manual:{order.makepay_payment_uid}:{remote.status}:"
                        f"{remote.session_id or 'none'}"
                    ),
                    makepay_status=remote.status,
                    payment_uid=order.makepay_payment_uid,
                    merchant_order_id=order.id,
                    session_id=remote.session_id,
                    event_type="manual_reconciliation",
                )
                order = result.order or order
            except MakePayAPIError:
                logger.warning("Manual status refresh failed for order %s", order.id)
        await query.answer(f"Order #{order.id[:8]} is {_status_label(order.status)}.")

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(
            "Unhandled Telegram update error",
            exc_info=context.error,
            extra={"update_type": type(update).__name__},
        )

    async def _require_private_chat(self, update: Update) -> bool:
        chat = update.effective_chat
        message = update.effective_message
        if chat is not None and chat.type == ChatType.PRIVATE:
            return True
        if message is not None:
            await message.reply_text(
                "For privacy, purchases are available only in a private chat with this bot."
            )
        return False

    async def _send_checkout(self, message: Message, order: Order) -> None:
        if not order.checkout_url:
            return
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Pay securely with MakePay", url=order.checkout_url)],
                [InlineKeyboardButton("Check payment status", callback_data=f"status:{order.id}")],
            ]
        )
        await message.reply_text(
            (
                f"<b>{html.escape(order.product_name)}</b>\n"
                f"Total: {_money(order.amount, order.fiat_currency)}\n"
                f"Order: <code>{order.id}</code>\n\n"
                "Use the button below. Never share a seed phrase or private key."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


def build_telegram_application(
    *,
    token: str,
    catalog: dict[str, Product],
    database: Database,
    makepay: MakePayClient,
) -> TelegramApplication:
    application = ApplicationBuilder().token(token).concurrent_updates(False).build()
    handlers = BotHandlers(catalog=catalog, database=database, makepay=makepay)
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help))
    application.add_handler(CommandHandler("shop", handlers.shop))
    application.add_handler(CommandHandler("orders", handlers.orders))
    application.add_handler(CallbackQueryHandler(handlers.buy, pattern=r"^buy:[a-z0-9_-]{1,32}$"))
    application.add_handler(
        CallbackQueryHandler(
            handlers.status,
            pattern=r"^status:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
    )
    application.add_error_handler(handlers.error)
    return application


BOT_COMMANDS = [
    BotCommand("shop", "Browse products"),
    BotCommand("orders", "View your latest orders"),
    BotCommand("help", "Payment safety and commands"),
]
