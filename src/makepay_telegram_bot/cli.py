from __future__ import annotations

import argparse
import asyncio

import uvicorn
from telegram import Bot

from .config import Settings


async def _set_telegram_webhook(settings: Settings) -> None:
    async with Bot(settings.telegram_bot_token) as bot:
        await bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret,
            drop_pending_updates=False,
        )
    print(f"Telegram webhook set to {settings.telegram_webhook_url}")


async def _delete_telegram_webhook(settings: Settings) -> None:
    async with Bot(settings.telegram_bot_token) as bot:
        await bot.delete_webhook(drop_pending_updates=False)
    print("Telegram webhook deleted; pending updates were preserved.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MakePay Telegram bot")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="Run the webhook server")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container listener
    serve.add_argument("--port", type=int, default=8000)
    subparsers.add_parser("set-telegram-webhook", help="Register the Telegram webhook")
    subparsers.add_parser("delete-telegram-webhook", help="Delete the Telegram webhook")
    arguments = parser.parse_args()

    if arguments.command in {None, "serve"}:
        uvicorn.run(
            "makepay_telegram_bot.app:app",
            host=getattr(arguments, "host", "0.0.0.0"),  # noqa: S104 - container listener
            port=getattr(arguments, "port", 8000),
        )
        return

    settings = Settings.from_env()
    if arguments.command == "set-telegram-webhook":
        asyncio.run(_set_telegram_webhook(settings))
    elif arguments.command == "delete-telegram-webhook":
        asyncio.run(_delete_telegram_webhook(settings))


if __name__ == "__main__":
    main()
