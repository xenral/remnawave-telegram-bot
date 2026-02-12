"""Shared CryptoBot constants and helpers used across bot, cabinet, and miniapp."""

import math

CRYPTOBOT_MIN_USD = 1.0
CRYPTOBOT_MAX_USD = 1000.0
CRYPTOBOT_FALLBACK_RATE = 95.0


def compute_cryptobot_limits(rate: float) -> tuple[int, int]:
    """Compute min/max kopeks for CryptoBot based on USD/base-currency rate."""
    min_kopeks = max(1, int(math.ceil(rate * CRYPTOBOT_MIN_USD * 100)))
    max_kopeks = int(math.floor(rate * CRYPTOBOT_MAX_USD * 100))
    max_kopeks = max(max_kopeks, min_kopeks)
    return min_kopeks, max_kopeks


async def get_usd_to_base_rate() -> float:
    """Fetch USD→base-currency rate with fallback."""
    from app.utils.currency_converter import currency_converter

    try:
        rate = await currency_converter.get_usd_to_rub_rate()
    except Exception:
        rate = 0.0
    if not rate or rate <= 0:
        rate = CRYPTOBOT_FALLBACK_RATE
    return float(rate)


def format_amount_with_currency(amount_kopeks: int, currency: str) -> str:
    """Simple amount formatter: '123.45 RUB'."""
    return f'{amount_kopeks / 100:.2f} {(currency or "RUB").upper()}'
