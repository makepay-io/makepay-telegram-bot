from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Notification, Order, Product, StatusUpdate

_PAID_STATUSES = {"complete", "completed", "confirmed", "paid", "succeeded", "success"}
_PROCESSING_STATUSES = {
    "confirming",
    "deposit_received",
    "paying",
    "processing",
    "sending",
    "swapping",
    "verifying",
    "waiting_confirmation",
}
_PENDING_STATUSES = {"awaiting_deposit", "created", "new", "pending", "quoted", "waiting"}
_CANCELLED_STATUSES = {"cancel", "canceled", "cancelled"}
_FAILED_STATUSES = {"chargeback", "fail", "failed"}
_TERMINAL_ORDER_STATUSES = {"paid", "cancelled", "expired", "failed"}
_NOTIFIABLE_STATUSES = {"processing", "underpaid", "paid", "cancelled", "expired", "failed"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def normalize_order_status(makepay_status: str) -> str:
    normalized = makepay_status.strip().lower()
    if normalized in _PAID_STATUSES:
        return "paid"
    if normalized in _PROCESSING_STATUSES:
        return "processing"
    if normalized in _PENDING_STATUSES:
        return "pending"
    if normalized in _CANCELLED_STATUSES:
        return "cancelled"
    if normalized in _FAILED_STATUSES:
        return "failed"
    if normalized == "expired":
        return "expired"
    if normalized == "underpaid":
        return "underpaid"
    return "pending"


def _next_status(current: str, incoming: str) -> str:
    if current == "paid" or current == incoming:
        return current
    if incoming == "paid":
        return "paid"
    if current in _TERMINAL_ORDER_STATUSES:
        return current
    if current == "underpaid" and incoming in {"pending", "processing"}:
        return current
    return incoming


def _order_from_row(row: sqlite3.Row) -> Order:
    return Order(
        id=str(row["id"]),
        request_key=str(row["request_key"]),
        telegram_chat_id=int(row["telegram_chat_id"]),
        telegram_user_id=int(row["telegram_user_id"]),
        product_id=str(row["product_id"]),
        product_name=str(row["product_name"]),
        amount=str(row["amount"]),
        fiat_currency=str(row["fiat_currency"]),
        status=str(row["status"]),
        makepay_status=str(row["makepay_status"]) if row["makepay_status"] else None,
        makepay_payment_uid=(
            str(row["makepay_payment_uid"]) if row["makepay_payment_uid"] else None
        ),
        checkout_url=str(row["checkout_url"]) if row["checkout_url"] else None,
        makepay_session_id=(str(row["makepay_session_id"]) if row["makepay_session_id"] else None),
        last_error=str(row["last_error"]) if row["last_error"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class Database:
    """Small, transaction-safe SQLite order and notification store."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fiat_currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    makepay_status TEXT,
                    makepay_payment_uid TEXT UNIQUE,
                    checkout_url TEXT,
                    makepay_session_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS orders_owner_created_idx
                    ON orders (telegram_user_id, telegram_chat_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS orders_status_updated_idx
                    ON orders (status, updated_at);

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    dedupe_key TEXT PRIMARY KEY,
                    delivery_id TEXT,
                    event_type TEXT,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(order_id, status)
                );
                """
            )
            self._connection.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ping(self) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1").fetchone() is not None

    def create_or_get_order(
        self,
        *,
        request_key: str,
        telegram_chat_id: int,
        telegram_user_id: int,
        product: Product,
    ) -> Order:
        now = utc_now()
        order_id = str(uuid4())
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO orders (
                    id, request_key, telegram_chat_id, telegram_user_id,
                    product_id, product_name, amount, fiat_currency, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?)
                """,
                (
                    order_id,
                    request_key,
                    telegram_chat_id,
                    telegram_user_id,
                    product.id,
                    product.name,
                    product.amount,
                    product.fiat_currency,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM orders WHERE request_key = ?", (request_key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Unable to create or load order.")
        return _order_from_row(row)

    def attach_payment_link(self, order_id: str, uid: str, checkout_url: str) -> Order:
        now = utc_now()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE orders
                SET makepay_payment_uid = COALESCE(makepay_payment_uid, ?),
                    checkout_url = COALESCE(checkout_url, ?),
                    status = CASE WHEN status IN ('creating', 'creation_failed')
                                  THEN 'pending' ELSE status END,
                    makepay_status = COALESCE(makepay_status, 'pending'),
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND (makepay_payment_uid IS NULL OR makepay_payment_uid = ?)
                """,
                (uid, checkout_url, now, order_id, uid),
            )
            if updated.rowcount == 0:
                existing = connection.execute(
                    "SELECT makepay_payment_uid FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if existing is None:
                    raise LookupError("Order not found.")
                raise RuntimeError("Order is already linked to a different MakePay payment.")
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:  # pragma: no cover - guarded inside the transaction
            raise RuntimeError("Unable to reload order.")
        return _order_from_row(row)

    def mark_creation_failed(self, order_id: str, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE orders
                SET status = CASE WHEN checkout_url IS NULL THEN 'creation_failed' ELSE status END,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_code[:200], utc_now(), order_id),
            )

    def get_order_for_owner(
        self, order_id: str, telegram_chat_id: int, telegram_user_id: int
    ) -> Order | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE id = ? AND telegram_chat_id = ? AND telegram_user_id = ?
                """,
                (order_id, telegram_chat_id, telegram_user_id),
            ).fetchone()
        return _order_from_row(row) if row else None

    def list_orders(
        self, telegram_chat_id: int, telegram_user_id: int, *, limit: int = 5
    ) -> list[Order]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE telegram_chat_id = ? AND telegram_user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (telegram_chat_id, telegram_user_id, limit),
            ).fetchall()
        return [_order_from_row(row) for row in rows]

    def list_reconcilable_orders(self, *, limit: int = 50) -> list[Order]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM orders
                WHERE makepay_payment_uid IS NOT NULL
                  AND status IN ('pending', 'processing', 'underpaid')
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_order_from_row(row) for row in rows]

    def apply_payment_status(
        self,
        *,
        dedupe_key: str,
        makepay_status: str,
        payment_uid: str | None,
        merchant_order_id: str | None,
        session_id: str | None,
        delivery_id: str | None = None,
        event_type: str | None = None,
    ) -> StatusUpdate:
        normalized_makepay_status = makepay_status.strip().lower()
        incoming_status = normalize_order_status(normalized_makepay_status)
        now = utc_now()
        with self._transaction() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO webhook_deliveries
                    (dedupe_key, delivery_id, event_type, received_at)
                VALUES (?, ?, ?, ?)
                """,
                (dedupe_key, delivery_id, event_type, now),
            ).rowcount
            if inserted == 0:
                return StatusUpdate(order=None, changed=False, duplicate=True)

            row: sqlite3.Row | None = None
            if payment_uid:
                row = connection.execute(
                    "SELECT * FROM orders WHERE makepay_payment_uid = ?", (payment_uid,)
                ).fetchone()
            if row is None and merchant_order_id:
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (merchant_order_id,)
                ).fetchone()
            if row is None:
                return StatusUpdate(order=None, changed=False, duplicate=False)

            current = _order_from_row(row)
            next_status = _next_status(current.status, incoming_status)
            changed = next_status != current.status
            connection.execute(
                """
                UPDATE orders
                SET status = ?,
                    makepay_status = ?,
                    makepay_payment_uid = COALESCE(makepay_payment_uid, ?),
                    makepay_session_id = COALESCE(?, makepay_session_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    normalized_makepay_status,
                    payment_uid,
                    session_id,
                    now,
                    current.id,
                ),
            )
            if changed and next_status in _NOTIFIABLE_STATUSES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notifications
                        (order_id, status, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (current.id, next_status, now),
                )
            updated_row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (current.id,)
            ).fetchone()
        return StatusUpdate(
            order=_order_from_row(updated_row) if updated_row else None,
            changed=changed,
            duplicate=False,
        )

    def pending_notifications(self, *, limit: int = 20) -> list[Notification]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT n.id, n.order_id, n.status, o.telegram_chat_id,
                       o.product_name, o.amount, o.fiat_currency, o.checkout_url
                FROM notifications AS n
                JOIN orders AS o ON o.id = n.order_id
                WHERE n.sent_at IS NULL
                ORDER BY n.created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            Notification(
                id=int(row["id"]),
                order_id=str(row["order_id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                product_name=str(row["product_name"]),
                amount=str(row["amount"]),
                fiat_currency=str(row["fiat_currency"]),
                status=str(row["status"]),
                checkout_url=str(row["checkout_url"]) if row["checkout_url"] else None,
            )
            for row in rows
        ]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE notifications
                SET sent_at = ?, attempts = attempts + 1, last_error = NULL
                WHERE id = ?
                """,
                (utc_now(), notification_id),
            )

    def mark_notification_failed(self, notification_id: int, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE notifications
                SET attempts = attempts + 1, last_error = ?
                WHERE id = ?
                """,
                (error_code[:200], notification_id),
            )
