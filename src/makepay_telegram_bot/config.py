from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_TELEGRAM_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class ConfigurationError(ValueError):
    """Raised when runtime configuration is missing or unsafe."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required.")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _https_url(value: str, name: str, *, allow_localhost_http: bool = False) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    local_http = (
        allow_localhost_http
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
    )
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(f"{name} must be a clean HTTPS origin.")
    return normalized


def _admin_ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    try:
        parsed = frozenset(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as error:
        raise ConfigurationError("ADMIN_TELEGRAM_USER_IDS must contain integer IDs.") from error
    if any(user_id <= 0 for user_id in parsed):
        raise ConfigurationError("ADMIN_TELEGRAM_USER_IDS must contain positive IDs.")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    public_base_url: str
    makepay_key_id: str
    makepay_key_secret: str
    makepay_webhook_secret: str
    makepay_api_base_url: str
    makepay_checkout_base_url: str
    database_path: Path
    catalog_path: Path
    admin_telegram_user_ids: frozenset[int]
    reconcile_interval_seconds: int
    notification_interval_seconds: int
    max_webhook_body_bytes: int
    log_level: str

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/telegram"

    @property
    def makepay_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/makepay"

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if source is None else source
        allow_localhost = env.get("ALLOW_INSECURE_LOCALHOST", "").lower() == "true"
        telegram_secret = _required(env, "TELEGRAM_WEBHOOK_SECRET")
        if not _TELEGRAM_SECRET_PATTERN.fullmatch(telegram_secret):
            raise ConfigurationError(
                "TELEGRAM_WEBHOOK_SECRET may contain only A-Z, a-z, 0-9, _ and - "
                "and must be 1-256 characters."
            )

        log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is invalid.")

        return cls(
            telegram_bot_token=_required(env, "TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=telegram_secret,
            public_base_url=_https_url(
                _required(env, "PUBLIC_BASE_URL"),
                "PUBLIC_BASE_URL",
                allow_localhost_http=allow_localhost,
            ),
            makepay_key_id=_required(env, "MAKEPAY_KEY_ID"),
            makepay_key_secret=_required(env, "MAKEPAY_KEY_SECRET"),
            makepay_webhook_secret=_required(env, "MAKEPAY_WEBHOOK_SECRET"),
            makepay_api_base_url=_https_url(
                env.get("MAKEPAY_API_BASE_URL", "https://www.makecrypto.io"),
                "MAKEPAY_API_BASE_URL",
                allow_localhost_http=allow_localhost,
            ),
            makepay_checkout_base_url=_https_url(
                env.get("MAKEPAY_CHECKOUT_BASE_URL", "https://www.makepay.io"),
                "MAKEPAY_CHECKOUT_BASE_URL",
                allow_localhost_http=allow_localhost,
            ),
            database_path=Path(env.get("DATABASE_PATH", "./data/bot.db")).expanduser(),
            catalog_path=Path(env.get("CATALOG_PATH", "./catalog.json")).expanduser(),
            admin_telegram_user_ids=_admin_ids(env.get("ADMIN_TELEGRAM_USER_IDS", "")),
            reconcile_interval_seconds=_positive_int(env, "RECONCILE_INTERVAL_SECONDS", 60, 10),
            notification_interval_seconds=_positive_int(
                env, "NOTIFICATION_INTERVAL_SECONDS", 10, 1
            ),
            max_webhook_body_bytes=_positive_int(env, "MAX_WEBHOOK_BODY_BYTES", 1_048_576, 1024),
            log_level=log_level,
        )
