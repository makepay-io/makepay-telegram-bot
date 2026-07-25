from pathlib import Path

import pytest

from makepay_telegram_bot.config import ConfigurationError, Settings


def valid_environment() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
        "TELEGRAM_WEBHOOK_SECRET": "telegram_secret-123",
        "PUBLIC_BASE_URL": "https://bot.example.com",
        "MAKEPAY_KEY_ID": "key-id",
        "MAKEPAY_KEY_SECRET": "key-secret",
        "MAKEPAY_WEBHOOK_SECRET": "mkwhsec_example",
        "DATABASE_PATH": "./state/orders.db",
        "CATALOG_PATH": "./catalog.json",
    }


def test_loads_settings_and_derived_webhook_urls() -> None:
    settings = Settings.from_env(valid_environment())

    assert settings.telegram_webhook_url == "https://bot.example.com/webhooks/telegram"
    assert settings.makepay_webhook_url == "https://bot.example.com/webhooks/makepay"
    assert settings.database_path == Path("./state/orders.db")
    assert settings.makepay_api_base_url == "https://www.makecrypto.io"
    assert settings.reconcile_interval_seconds == 60


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PUBLIC_BASE_URL", "http://bot.example.com", "HTTPS origin"),
        ("PUBLIC_BASE_URL", "https://user:pass@bot.example.com", "HTTPS origin"),
        ("PUBLIC_BASE_URL", "https://bot.example.com/hidden-path", "HTTPS origin"),
        ("PUBLIC_BASE_URL", "https://bot.example.com?token=bad", "HTTPS origin"),
        ("TELEGRAM_WEBHOOK_SECRET", "spaces are unsafe", "may contain only"),
        ("RECONCILE_INTERVAL_SECONDS", "zero", "integer"),
        ("RECONCILE_INTERVAL_SECONDS", "2", "at least 10"),
        ("LOG_LEVEL", "LOUD", "invalid"),
    ],
)
def test_rejects_unsafe_or_invalid_settings(name: str, value: str, message: str) -> None:
    environment = valid_environment()
    environment[name] = value
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env(environment)


def test_allows_explicit_local_http_for_development() -> None:
    environment = valid_environment()
    environment["PUBLIC_BASE_URL"] = "http://localhost:8000"
    environment["MAKEPAY_API_BASE_URL"] = "http://127.0.0.1:8001"
    environment["ALLOW_INSECURE_LOCALHOST"] = "true"

    settings = Settings.from_env(environment)

    assert settings.public_base_url == "http://localhost:8000"


def test_parses_admin_ids() -> None:
    environment = valid_environment()
    environment["ADMIN_TELEGRAM_USER_IDS"] = "123, 456"

    assert Settings.from_env(environment).admin_telegram_user_ids == {123, 456}


def test_rejects_non_positive_admin_ids() -> None:
    environment = valid_environment()
    environment["ADMIN_TELEGRAM_USER_IDS"] = "-1"

    with pytest.raises(ConfigurationError, match="positive"):
        Settings.from_env(environment)
