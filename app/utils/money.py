from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


DEFAULT_CURRENCY = 'RUB'
DEFAULT_REPORTING_CURRENCY = 'RUB'


@dataclass(frozen=True, slots=True)
class CurrencyMeta:
    code: str
    exponent: int
    symbol: str


_CURRENCIES: dict[str, CurrencyMeta] = {
    'RUB': CurrencyMeta(code='RUB', exponent=2, symbol='₽'),
    'USD': CurrencyMeta(code='USD', exponent=2, symbol='USD'),
    'EUR': CurrencyMeta(code='EUR', exponent=2, symbol='EUR'),
    'IRR': CurrencyMeta(code='IRR', exponent=0, symbol='IRR'),
    # TMN is display-only alias over IRR (1 TMN = 10 IRR)
    'TMN': CurrencyMeta(code='TMN', exponent=0, symbol='TMN'),
}


def normalize_currency(code: str | None, default: str = DEFAULT_CURRENCY) -> str:
    normalized = (code or '').strip().upper()
    if not normalized:
        return default
    return normalized


def get_currency_meta(code: str | None) -> CurrencyMeta:
    normalized = normalize_currency(code)
    return _CURRENCIES.get(normalized, CurrencyMeta(code=normalized, exponent=2, symbol=normalized))


def _quantize_major(value: Decimal, exponent: int) -> Decimal:
    if exponent <= 0:
        return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal(10) ** (-exponent), rounding=ROUND_HALF_UP)


def minor_to_major(amount_minor: int, currency: str | None) -> Decimal:
    meta = get_currency_meta(currency)
    if meta.exponent <= 0:
        return Decimal(amount_minor)
    return Decimal(amount_minor) / (Decimal(10) ** meta.exponent)


def major_to_minor(amount_major: Decimal | float | int, currency: str | None) -> int:
    meta = get_currency_meta(currency)
    amount = Decimal(str(amount_major))
    if meta.exponent <= 0:
        return int(amount.to_integral_value(rounding=ROUND_HALF_UP))
    scaled = amount * (Decimal(10) ** meta.exponent)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def format_money_from_minor(
    amount_minor: int,
    currency: str | None = None,
    *,
    display_currency: str | None = None,
    round_minor: bool = False,
) -> str:
    base_currency = normalize_currency(currency)
    target_currency = normalize_currency(display_currency, default=base_currency)

    # TMN is a display adapter over IRR.
    if base_currency == 'IRR' and target_currency == 'TMN':
        value_tmn = Decimal(amount_minor) / Decimal(10)
        if round_minor:
            value_tmn = value_tmn.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        else:
            value_tmn = value_tmn.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP).normalize()
        value = f'{value_tmn:f}'
        if '.' in value:
            value = value.rstrip('0').rstrip('.')
        return f'{value} TMN'

    meta = get_currency_meta(target_currency)
    major = minor_to_major(amount_minor, target_currency)
    if round_minor:
        major = _quantize_major(major, 0)
    elif meta.exponent > 0:
        major = _quantize_major(major, meta.exponent)

    value = f'{major:f}'
    if '.' in value:
        value = value.rstrip('0').rstrip('.')
    return f'{value} {meta.symbol}'


def convert_minor_with_rate(
    amount_minor: int,
    *,
    from_currency: str | None,
    to_currency: str | None,
    rate: Decimal,
) -> int:
    source = normalize_currency(from_currency)
    target = normalize_currency(to_currency)
    if source == target:
        return amount_minor

    source_major = minor_to_major(amount_minor, source)
    target_major = source_major * rate
    return major_to_minor(target_major, target)
