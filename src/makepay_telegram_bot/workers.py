from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .database import Database
from .makepay_client import MakePayAPIError, MakePayClient
from .models import Notification
from .telegram_types import TelegramApplication

logger = logging.getLogger(__name__)


def _notification_text(notification: Notification) -> str:
    product = html.escape(notification.product_name)
    order = f"<code>{notification.order_id}</code>"
    messages = {
        "processing": f"Payment received for <b>{product}</b> and is processing.\nOrder: {order}",
        "underpaid": (f"Order {order} is underpaid. Open checkout to review the remaining amount."),
        "paid": f"✅ Payment complete for <b>{product}</b>.\nOrder: {order}",
        "expired": f"Payment window expired for <b>{product}</b>.\nOrder: {order}",
        "cancelled": f"Payment was cancelled for <b>{product}</b>.\nOrder: {order}",
        "failed": f"Payment failed for <b>{product}</b>.\nOrder: {order}",
    }
    return messages.get(notification.status, f"Order {order}: {notification.status}.")


async def notification_loop(
    *,
    application: TelegramApplication,
    database: Database,
    wakeup: asyncio.Event,
    interval_seconds: int,
) -> None:
    while True:
        for notification in database.pending_notifications():
            keyboard = None
            if notification.checkout_url and notification.status in {"underpaid", "processing"}:
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Open MakePay checkout", url=notification.checkout_url)]]
                )
            try:
                await application.bot.send_message(
                    notification.telegram_chat_id,
                    _notification_text(notification),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                database.mark_notification_sent(notification.id)
            except TelegramError as error:
                logger.warning(
                    "Telegram notification failed for order %s: %s",
                    notification.order_id,
                    type(error).__name__,
                )
                database.mark_notification_failed(
                    notification.id, f"telegram_{type(error).__name__}"
                )
        wakeup.clear()
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def reconciliation_loop(
    *,
    makepay: MakePayClient,
    database: Database,
    notification_wakeup: asyncio.Event,
    interval_seconds: int,
) -> None:
    while True:
        for order in database.list_reconcilable_orders():
            if not order.makepay_payment_uid:
                continue
            try:
                remote = await makepay.get_payment_status(order.makepay_payment_uid)
                result = database.apply_payment_status(
                    dedupe_key=(
                        f"reconcile:{order.makepay_payment_uid}:{remote.status}:"
                        f"{remote.session_id or 'none'}"
                    ),
                    makepay_status=remote.status,
                    payment_uid=order.makepay_payment_uid,
                    merchant_order_id=order.id,
                    session_id=remote.session_id,
                    event_type="scheduled_reconciliation",
                )
                if result.changed:
                    notification_wakeup.set()
            except MakePayAPIError as error:
                logger.warning(
                    "MakePay reconciliation failed for order %s with status %s",
                    order.id,
                    error.status_code,
                )
        await asyncio.sleep(interval_seconds)
