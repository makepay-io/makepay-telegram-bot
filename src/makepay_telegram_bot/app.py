from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from makepay import MakePayError, parse_makepay_webhook
from telegram import Update

from . import __version__
from .bot import BOT_COMMANDS, build_telegram_application
from .catalog import load_catalog
from .config import Settings
from .database import Database
from .makepay_client import MakePayClient
from .telegram_types import TelegramApplication
from .webhooks import extract_makepay_event
from .workers import notification_loop, reconciliation_loop

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    makepay: MakePayClient
    telegram: TelegramApplication
    notification_wakeup: asyncio.Event


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _body_too_large(request: Request, limit: int) -> bool:
    value = request.headers.get("content-length")
    if value is None:
        return False
    try:
        return int(value) > limit
    except ValueError:
        return True


def _runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, Runtime):
        raise HTTPException(status_code=503, detail="Service is starting.")
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    _configure_logging(settings.log_level)
    catalog = load_catalog(settings.catalog_path)
    database = Database(settings.database_path)
    makepay = MakePayClient(
        key_id=settings.makepay_key_id,
        key_secret=settings.makepay_key_secret,
        api_base_url=settings.makepay_api_base_url,
        checkout_base_url=settings.makepay_checkout_base_url,
    )
    telegram = build_telegram_application(
        token=settings.telegram_bot_token,
        catalog=catalog,
        database=database,
        makepay=makepay,
    )
    notification_wakeup = asyncio.Event()
    await telegram.initialize()
    await telegram.bot.set_my_commands(BOT_COMMANDS)
    await telegram.bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    await telegram.start()
    app.state.runtime = Runtime(
        settings=settings,
        database=database,
        makepay=makepay,
        telegram=telegram,
        notification_wakeup=notification_wakeup,
    )
    tasks = [
        asyncio.create_task(
            notification_loop(
                application=telegram,
                database=database,
                wakeup=notification_wakeup,
                interval_seconds=settings.notification_interval_seconds,
            ),
            name="notification-outbox",
        ),
        asyncio.create_task(
            reconciliation_loop(
                makepay=makepay,
                database=database,
                notification_wakeup=notification_wakeup,
                interval_seconds=settings.reconcile_interval_seconds,
            ),
            name="makepay-reconciliation",
        ),
    ]
    logger.info("MakePay Telegram bot started")
    try:
        yield
    finally:
        app.state.runtime = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await telegram.stop()
        await telegram.shutdown()
        await makepay.close()
        database.close()
        logger.info("MakePay Telegram bot stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MakePay Telegram Bot",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": "makepay-telegram-bot", "version": __version__}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        runtime = _runtime(request)
        if not runtime.database.ping() or not runtime.telegram.running:
            raise HTTPException(status_code=503, detail="Service is not ready.")
        return {"status": "ready"}

    @app.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> JSONResponse:
        runtime = _runtime(request)
        provided_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(
            provided_secret.encode(), runtime.settings.telegram_webhook_secret.encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret.")
        if _body_too_large(request, runtime.settings.max_webhook_body_bytes):
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        raw_body = await request.body()
        if len(raw_body) > runtime.settings.max_webhook_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        try:
            payload = json.loads(raw_body)
            update = Update.de_json(payload, runtime.telegram.bot)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Invalid Telegram update.") from error
        if runtime.telegram.update_queue.qsize() >= 1000:
            raise HTTPException(status_code=503, detail="Telegram update queue is full.")
        await runtime.telegram.update_queue.put(update)
        return JSONResponse({"ok": True})

    @app.post("/webhooks/makepay")
    async def makepay_webhook(request: Request) -> JSONResponse:
        runtime = _runtime(request)
        if _body_too_large(request, runtime.settings.max_webhook_body_bytes):
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        raw_body = await request.body()
        if len(raw_body) > runtime.settings.max_webhook_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body is too large.")
        try:
            payload = parse_makepay_webhook(
                raw_body,
                request.headers.get("x-makepay-signature"),
                runtime.settings.makepay_webhook_secret,
            )
        except MakePayError as error:
            raise HTTPException(
                status_code=error.status if error.status in {400, 401} else 401,
                detail="Invalid MakePay webhook.",
            ) from error

        event = extract_makepay_event(
            payload,
            {key.lower(): value for key, value in request.headers.items()},
            raw_body,
        )
        if event.status is None:
            return JSONResponse({"ok": True, "matched": False})
        result = runtime.database.apply_payment_status(
            dedupe_key=event.dedupe_key,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            makepay_status=event.status,
            payment_uid=event.payment_uid,
            merchant_order_id=event.merchant_order_id,
            session_id=event.session_id,
        )
        if result.changed:
            runtime.notification_wakeup.set()
        return JSONResponse(
            {
                "ok": True,
                "matched": result.order is not None,
                "duplicate": result.duplicate,
            }
        )

    return app


app = create_app()
