from pathlib import Path

import pytest

from makepay_telegram_bot.database import Database, normalize_order_status
from makepay_telegram_bot.models import Product

PRODUCT = Product(
    id="coffee",
    name="Coffee voucher",
    description="A demo product",
    amount="5.00",
    fiat_currency="USD",
)


def make_order(database: Database) -> str:
    order = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )
    database.attach_payment_link(order.id, "pay_123", "https://www.makepay.io/payment/pay_123")
    return order.id


def test_order_creation_is_idempotent_and_owner_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "orders.db")
    first = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )
    second = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )

    assert first.id == second.id
    assert database.get_order_for_owner(first.id, 100, 200) == first
    assert database.get_order_for_owner(first.id, 100, 999) is None
    database.close()


def test_status_updates_are_deduplicated_and_create_outbox_notifications(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "orders.db")
    order_id = make_order(database)

    processing = database.apply_payment_status(
        dedupe_key="group:processing",
        delivery_id="delivery-1",
        event_type="makepay.payment.status_changed",
        makepay_status="deposit_received",
        payment_uid="pay_123",
        merchant_order_id=order_id,
        session_id="session_123",
    )
    duplicate = database.apply_payment_status(
        dedupe_key="group:processing",
        delivery_id="delivery-2",
        event_type="makepay.payment.status_changed",
        makepay_status="deposit_received",
        payment_uid="pay_123",
        merchant_order_id=order_id,
        session_id="session_123",
    )

    assert processing.changed is True
    assert processing.order is not None and processing.order.status == "processing"
    assert duplicate.duplicate is True
    notification = database.pending_notifications()[0]
    assert notification.status == "processing"
    database.mark_notification_failed(notification.id, "telegram_timeout")
    database.mark_notification_sent(notification.id)
    assert database.pending_notifications() == []
    database.close()


def test_paid_is_final_but_late_payment_can_recover_failed_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "orders.db")
    order_id = make_order(database)
    database.apply_payment_status(
        dedupe_key="failed",
        makepay_status="failed",
        payment_uid="pay_123",
        merchant_order_id=order_id,
        session_id="session_123",
    )
    recovered = database.apply_payment_status(
        dedupe_key="paid",
        makepay_status="complete",
        payment_uid="pay_123",
        merchant_order_id=order_id,
        session_id="session_123",
    )
    stale = database.apply_payment_status(
        dedupe_key="stale",
        makepay_status="expired",
        payment_uid="pay_123",
        merchant_order_id=order_id,
        session_id="session_123",
    )

    assert recovered.order is not None and recovered.order.status == "paid"
    assert stale.order is not None and stale.order.status == "paid"
    database.close()


def test_webhook_can_correlate_by_merchant_order_id_before_uid_is_attached(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "orders.db")
    order = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )

    result = database.apply_payment_status(
        dedupe_key="early",
        makepay_status="complete",
        payment_uid="pay_early",
        merchant_order_id=order.id,
        session_id="session_early",
    )

    assert result.order is not None
    assert result.order.makepay_payment_uid == "pay_early"
    assert result.order.status == "paid"
    database.close()


def test_unknown_webhook_is_acknowledged_without_order(tmp_path: Path) -> None:
    database = Database(tmp_path / "orders.db")
    result = database.apply_payment_status(
        dedupe_key="unknown",
        makepay_status="pending",
        payment_uid="missing",
        merchant_order_id=None,
        session_id=None,
    )

    assert result.order is None
    assert result.duplicate is False
    database.close()


def test_failed_creation_can_be_retried_and_pending_orders_are_reconcilable(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "orders.db")
    order = database.create_or_get_order(
        request_key="callback:1",
        telegram_chat_id=100,
        telegram_user_id=200,
        product=PRODUCT,
    )
    database.mark_creation_failed(order.id, "makepay_http_503")
    failed = database.get_order_for_owner(order.id, 100, 200)
    assert failed is not None and failed.status == "creation_failed"
    assert failed.last_error == "makepay_http_503"

    database.attach_payment_link(order.id, "pay_retry", "https://www.makepay.io/payment/pay_retry")
    assert database.list_reconcilable_orders()[0].id == order.id
    assert database.list_orders(100, 200, limit=1)[0].id == order.id
    database.close()


def test_payment_link_uid_cannot_change_after_early_webhook(tmp_path: Path) -> None:
    database = Database(tmp_path / "orders.db")
    order_id = make_order(database)

    with pytest.raises(RuntimeError, match="different MakePay payment"):
        database.attach_payment_link(
            order_id, "pay_different", "https://www.makepay.io/payment/pay_different"
        )
    with pytest.raises(LookupError, match="Order not found"):
        database.attach_payment_link(
            "missing", "pay_missing", "https://www.makepay.io/payment/pay_missing"
        )
    database.close()


def test_status_normalization() -> None:
    assert normalize_order_status("COMPLETE") == "paid"
    assert normalize_order_status("swapping") == "processing"
    assert normalize_order_status("underpaid") == "underpaid"
    assert normalize_order_status("cancelled") == "cancelled"
    assert normalize_order_status("unknown_future_status") == "pending"
