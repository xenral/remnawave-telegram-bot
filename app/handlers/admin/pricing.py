import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import SubscriptionPeriodPrice, TrafficPackagePrice, User
from app.localization.texts import get_texts
from app.services.system_settings_service import bot_configuration_service
from app.states import PricingStates
from app.utils.decorators import admin_required, error_handler
from app.utils.money import get_currency_meta, major_to_minor, normalize_currency


logger = logging.getLogger(__name__)


PriceItem = tuple[str, str, int]


TRAFFIC_PACKAGE_FIELDS: tuple[tuple[int, str], ...] = (
    (5, 'PRICE_TRAFFIC_5GB'),
    (10, 'PRICE_TRAFFIC_10GB'),
    (25, 'PRICE_TRAFFIC_25GB'),
    (50, 'PRICE_TRAFFIC_50GB'),
    (100, 'PRICE_TRAFFIC_100GB'),
    (250, 'PRICE_TRAFFIC_250GB'),
    (500, 'PRICE_TRAFFIC_500GB'),
    (1000, 'PRICE_TRAFFIC_1000GB'),
    (0, 'PRICE_TRAFFIC_UNLIMITED'),
)

TRAFFIC_PACKAGE_FIELD_MAP: dict[int, str] = {gb: field for gb, field in TRAFFIC_PACKAGE_FIELDS}
TRAFFIC_PACKAGE_ORDER: tuple[int, ...] = tuple(gb for gb, _ in TRAFFIC_PACKAGE_FIELDS)
TRAFFIC_PACKAGE_ORDER_INDEX: dict[int, int] = {gb: index for index, gb in enumerate(TRAFFIC_PACKAGE_ORDER)}


def _parse_supported_balance_currencies() -> list[str]:
    raw = getattr(settings, 'SUPPORTED_BALANCE_CURRENCIES', '') or ''
    candidates = [normalize_currency(item) for item in raw.split(',') if item.strip()]
    default_currency = normalize_currency(getattr(settings, 'DEFAULT_BALANCE_CURRENCY', 'RUB'))

    result: list[str] = []
    seen: set[str] = set()
    for code in [*candidates, default_currency]:
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)

    return result or [default_currency]


def _default_pricing_currency() -> str:
    return normalize_currency(getattr(settings, 'DEFAULT_BALANCE_CURRENCY', 'RUB'))


def _normalize_pricing_currency(value: str | None) -> str:
    supported = _parse_supported_balance_currencies()
    normalized = normalize_currency(value, default=_default_pricing_currency())
    if normalized in supported:
        return normalized
    return supported[0]


def _period_days_from_key(key: str) -> int | None:
    if not (key.startswith('PRICE_') and key.endswith('_DAYS')):
        return None
    try:
        return int(key.replace('PRICE_', '').replace('_DAYS', ''))
    except ValueError:
        return None


def _traffic_gb_from_key(key: str) -> int | None:
    if not key.startswith('PRICE_TRAFFIC_'):
        return None
    if key.endswith('UNLIMITED'):
        return 0
    digits = ''.join(ch for ch in key if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _is_currency_table_price_key(key: str) -> bool:
    return _period_days_from_key(key) is not None or _traffic_gb_from_key(key) is not None


def _get_price_input_currency(key: str, selected_pricing_currency: str) -> str:
    """
    Returns the currency that should be used to parse/display the editable numeric value.

    Per-currency catalog prices (period/traffic) use the currently selected pricing currency.
    Legacy/global price fields keep using default balance currency.
    """
    if settings.MULTI_CURRENCY_ENABLED and _is_currency_table_price_key(key):
        return _normalize_pricing_currency(selected_pricing_currency)
    return _default_pricing_currency()


def _format_price_for_currency(amount_minor: int, currency: str) -> str:
    normalized_currency = _normalize_pricing_currency(currency)
    return settings.format_price(
        int(amount_minor or 0),
        currency=normalized_currency,
        display_currency=normalized_currency,
    )


@dataclass(slots=True)
class ChoiceOption:
    value: Any
    label_ru: str
    label_en: str | None = None

    def label(self, lang_code: str) -> str:
        if lang_code == 'ru':
            return self.label_ru
        return self.label_en or self.label_ru


@dataclass(slots=True)
class SettingEntry:
    key: str
    section: str
    label_ru: str
    label_en: str
    action: str  # "input", "toggle", "price", "choice"
    description_ru: str | None = None
    description_en: str | None = None
    choices: tuple[ChoiceOption, ...] | None = None

    def label(self, lang_code: str) -> str:
        if lang_code == 'ru':
            return self.label_ru
        return self.label_en or self.label_ru

    def description(self, lang_code: str) -> str | None:
        if lang_code == 'ru':
            return self.description_ru
        return self.description_en or self.description_ru


TRIAL_ENTRIES: tuple[SettingEntry, ...] = (
    SettingEntry(
        key='TRIAL_DURATION_DAYS',
        section='trial',
        label_ru='⏳ Длительность (дни)',
        label_en='⏳ Duration (days)',
        action='input',
    ),
    SettingEntry(
        key='TRIAL_TRAFFIC_LIMIT_GB',
        section='trial',
        label_ru='📦 Лимит трафика (ГБ)',
        label_en='📦 Traffic limit (GB)',
        action='input',
    ),
    SettingEntry(
        key='TRIAL_DEVICE_LIMIT',
        section='trial',
        label_ru='📱 Лимит устройств',
        label_en='📱 Device limit',
        action='input',
    ),
    SettingEntry(
        key='TRIAL_PAYMENT_ENABLED',
        section='trial',
        label_ru='💳 Платная активация',
        label_en='💳 Paid activation',
        action='toggle',
        description_ru='Если включено — за активацию триала будет списываться указанная сумма.',
        description_en='When enabled, the configured amount is charged during trial activation.',
    ),
    SettingEntry(
        key='TRIAL_ACTIVATION_PRICE',
        section='trial',
        label_ru='💰 Стоимость активации',
        label_en='💰 Activation price',
        action='price',
        description_ru='Указывается в копейках. 0 — бесплатная активация.',
        description_en='Amount in kopeks. 0 — free activation.',
    ),
    SettingEntry(
        key='TRIAL_ADD_REMAINING_DAYS_TO_PAID',
        section='trial',
        label_ru='➕ Добавлять оставшиеся дни к платной подписке',
        label_en='➕ Add remaining trial days to paid plan',
        action='toggle',
        description_ru='Если включено — при покупке платной подписки оставшиеся дни триала будут добавлены к сроку.',
        description_en='When enabled, remaining trial days are added to paid subscription duration.',
    ),
)


CORE_PRICING_ENTRIES: tuple[SettingEntry, ...] = (
    SettingEntry(
        key='BASE_SUBSCRIPTION_PRICE',
        section='core',
        label_ru='💳 Базовая стоимость подписки',
        label_en='💳 Base subscription price',
        action='price',
    ),
    SettingEntry(
        key='BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED',
        section='core',
        label_ru='🎟️ Базовые скидки для групп',
        label_en='🎟️ Base group discounts',
        action='toggle',
        description_ru='Включает применение базовых скидок для групповых промо-периодов.',
        description_en='Enables base discounts for promo group periods.',
    ),
    SettingEntry(
        key='BASE_PROMO_GROUP_PERIOD_DISCOUNTS',
        section='core',
        label_ru='🔖 Скидки по периодам',
        label_en='🔖 Period discounts',
        action='input',
        description_ru='Формат: список пар дней и скидки через запятую (например 30:10,60:20).',
        description_en='Format: comma-separated day/discount pairs (e.g. 30:10,60:20).',
    ),
    SettingEntry(
        key='DEFAULT_DEVICE_LIMIT',
        section='core',
        label_ru='📱 Устройств по умолчанию',
        label_en='📱 Default device limit',
        action='input',
    ),
    SettingEntry(
        key='DEFAULT_TRAFFIC_LIMIT_GB',
        section='core',
        label_ru='📦 Трафик по умолчанию (ГБ)',
        label_en='📦 Default traffic (GB)',
        action='input',
    ),
    SettingEntry(
        key='MAX_DEVICES_LIMIT',
        section='core',
        label_ru='📈 Максимум устройств',
        label_en='📈 Maximum devices',
        action='input',
    ),
    SettingEntry(
        key='RESET_TRAFFIC_ON_PAYMENT',
        section='core',
        label_ru='🔄 Сбрасывать трафик при оплате',
        label_en='🔄 Reset traffic on payment',
        action='toggle',
    ),
    SettingEntry(
        key='DEFAULT_TRAFFIC_RESET_STRATEGY',
        section='core',
        label_ru='🗓 Стратегия сброса трафика',
        label_en='🗓 Traffic reset strategy',
        action='input',
        description_ru='Доступные значения: DAY, WEEK, MONTH, NEVER.',
        description_en='Available values: DAY, WEEK, MONTH, NEVER.',
    ),
    SettingEntry(
        key='TRAFFIC_SELECTION_MODE',
        section='core',
        label_ru='⚙️ Режим выбора трафика',
        label_en='⚙️ Traffic selection mode',
        action='choice',
        choices=(
            ChoiceOption('selectable', 'Выбор пакетов', 'Selectable'),
            ChoiceOption('fixed', 'Фиксированный лимит', 'Fixed limit'),
            ChoiceOption('fixed_with_topup', 'Фикс. лимит + докупка', 'Fixed + topup'),
        ),
        description_ru='Определяет, выбирают ли пользователи пакеты или получают фиксированный лимит.',
        description_en='Defines whether users pick packages or use a fixed limit.',
    ),
    SettingEntry(
        key='FIXED_TRAFFIC_LIMIT_GB',
        section='core',
        label_ru='📏 Фиксированный лимит трафика (ГБ)',
        label_en='📏 Fixed traffic limit (GB)',
        action='input',
        description_ru='Используется только в режиме фиксированного трафика. 0 = безлимит.',
        description_en='Used only in fixed traffic mode. 0 = unlimited.',
    ),
)


SETTING_ENTRIES_BY_SECTION: dict[str, tuple[SettingEntry, ...]] = {
    'trial': TRIAL_ENTRIES,
    'core': CORE_PRICING_ENTRIES,
}

SETTING_ENTRY_BY_KEY: dict[str, SettingEntry] = {
    entry.key: entry for entries in SETTING_ENTRIES_BY_SECTION.values() for entry in entries
}

SETTING_ENTRIES: tuple[SettingEntry, ...] = tuple(
    entry for entries in SETTING_ENTRIES_BY_SECTION.values() for entry in entries
)

SETTING_KEY_TO_TOKEN: dict[str, str] = {entry.key: f's{index}' for index, entry in enumerate(SETTING_ENTRIES)}

SETTING_TOKEN_TO_KEY: dict[str, str] = {token: key for key, token in SETTING_KEY_TO_TOKEN.items()}


def _encode_setting_callback_key(key: str) -> str:
    return SETTING_KEY_TO_TOKEN.get(key, key)


def _decode_setting_callback_key(raw: str) -> str:
    return SETTING_TOKEN_TO_KEY.get(raw, raw)


def _traffic_package_sort_key(package: dict[str, Any]) -> tuple[int, int]:
    order_index = TRAFFIC_PACKAGE_ORDER_INDEX.get(package['gb'])
    if order_index is not None:
        return (0, order_index)
    return (1, package['gb'])


def _collect_traffic_packages() -> list[dict[str, Any]]:
    raw_packages = settings.get_traffic_packages()

    packages_map: dict[int, dict[str, Any]] = {}
    for package in raw_packages:
        gb = int(package.get('gb', 0))
        packages_map[gb] = {
            'gb': gb,
            'price': int(package.get('price') or 0),
            'enabled': bool(package.get('enabled', True)),
            'field': TRAFFIC_PACKAGE_FIELD_MAP.get(gb),
        }

    for gb, field in TRAFFIC_PACKAGE_FIELDS:
        if not hasattr(settings, field):
            continue

        price = getattr(settings, field)
        existing = packages_map.get(gb)
        enabled = existing['enabled'] if existing is not None else True

        packages_map[gb] = {
            'gb': gb,
            'price': int(price),
            'enabled': enabled,
            'field': field,
        }

    packages = list(packages_map.values())
    packages.sort(key=_traffic_package_sort_key)
    return packages


def _serialize_traffic_packages(packages: Iterable[dict[str, Any]]) -> str:
    parts = []
    for package in packages:
        enabled_flag = 'true' if package.get('enabled') else 'false'
        parts.append(f'{int(package["gb"])}:{int(package["price"])}:{enabled_flag}')
    return ','.join(parts)


async def _save_traffic_packages(
    db: AsyncSession,
    packages: Iterable[dict[str, Any]],
    *,
    skip_if_same: bool = False,
) -> bool:
    new_value = _serialize_traffic_packages(packages)
    current_value = bot_configuration_service.get_current_value('TRAFFIC_PACKAGES_CONFIG') or ''

    if skip_if_same and current_value == new_value:
        return False

    await bot_configuration_service.set_value(db, 'TRAFFIC_PACKAGES_CONFIG', new_value)
    await db.commit()
    return True


async def _load_currency_price_maps(
    db: AsyncSession,
    currency: str,
) -> tuple[dict[int, int], dict[int, int]]:
    if not settings.MULTI_CURRENCY_ENABLED:
        return {}, {}

    normalized_currency = _normalize_pricing_currency(currency)

    period_result = await db.execute(
        select(SubscriptionPeriodPrice).where(
            SubscriptionPeriodPrice.currency == normalized_currency,
            SubscriptionPeriodPrice.is_active.is_(True),
        )
    )
    period_map = {int(row.period_days): int(row.amount_minor) for row in period_result.scalars().all()}

    traffic_result = await db.execute(
        select(TrafficPackagePrice).where(
            TrafficPackagePrice.currency == normalized_currency,
            TrafficPackagePrice.is_active.is_(True),
        )
    )
    traffic_map = {int(row.package_gb): int(row.amount_minor) for row in traffic_result.scalars().all()}

    return period_map, traffic_map


async def _resolve_price_value_minor(
    db: AsyncSession,
    key: str,
    currency: str,
) -> int:
    normalized_currency = _normalize_pricing_currency(currency)

    if settings.MULTI_CURRENCY_ENABLED:
        period_days = _period_days_from_key(key)
        if period_days is not None:
            result = await db.execute(
                select(SubscriptionPeriodPrice).where(
                    SubscriptionPeriodPrice.period_days == period_days,
                    SubscriptionPeriodPrice.currency == normalized_currency,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return int(row.amount_minor)

        package_gb = _traffic_gb_from_key(key)
        if package_gb is not None:
            result = await db.execute(
                select(TrafficPackagePrice).where(
                    TrafficPackagePrice.package_gb == package_gb,
                    TrafficPackagePrice.currency == normalized_currency,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return int(row.amount_minor)

    return int(getattr(settings, key, 0) or 0)


async def _upsert_currency_price(
    db: AsyncSession,
    key: str,
    currency: str,
    amount_minor: int,
) -> bool:
    normalized_currency = _normalize_pricing_currency(currency)
    period_days = _period_days_from_key(key)
    if period_days is not None:
        result = await db.execute(
            select(SubscriptionPeriodPrice).where(
                SubscriptionPeriodPrice.period_days == period_days,
                SubscriptionPeriodPrice.currency == normalized_currency,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = SubscriptionPeriodPrice(
                period_days=period_days,
                currency=normalized_currency,
                amount_minor=amount_minor,
                is_active=True,
            )
            db.add(row)
        else:
            row.amount_minor = amount_minor
            row.is_active = True
        return True

    package_gb = _traffic_gb_from_key(key)
    if package_gb is not None:
        result = await db.execute(
            select(TrafficPackagePrice).where(
                TrafficPackagePrice.package_gb == package_gb,
                TrafficPackagePrice.currency == normalized_currency,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = TrafficPackagePrice(
                package_gb=package_gb,
                currency=normalized_currency,
                amount_minor=amount_minor,
                is_active=True,
            )
            db.add(row)
        else:
            row.amount_minor = amount_minor
            row.is_active = True
        return True

    return False


def _language_code(language: str | None) -> str:
    return (language or 'ru').split('-')[0].lower()


def _format_period_label(days: int, lang_code: str, short: bool = False) -> str:
    if short:
        suffix = 'д' if lang_code == 'ru' else 'd'
        return f'{days}{suffix}'
    if lang_code == 'ru':
        return f'{days} дней'
    if days == 1:
        return '1 day'
    return f'{days}-day plan'


def _format_traffic_label(gb: int, lang_code: str, short: bool = False) -> str:
    if gb == 0:
        return '∞' if short else ('Безлимит' if lang_code == 'ru' else 'Unlimited')
    unit = 'ГБ' if lang_code == 'ru' else 'GB'
    if short:
        return f'{gb}{unit}' if lang_code == 'ru' else f'{gb}{unit}'
    return f'{gb} {unit}'


def _format_trial_summary(lang_code: str, pricing_currency: str) -> str:
    duration = settings.TRIAL_DURATION_DAYS
    traffic = settings.TRIAL_TRAFFIC_LIMIT_GB
    devices = settings.TRIAL_DEVICE_LIMIT
    price_note = ''
    if settings.is_trial_paid_activation_enabled():
        price_note = f', 💳 {_format_price_for_currency(settings.get_trial_activation_price(), pricing_currency)}'

    traffic_label = _format_traffic_label(traffic, lang_code, short=True)
    devices_label = f'{devices}📱' if lang_code == 'ru' else f'{devices}📱'
    days_suffix = 'д' if lang_code == 'ru' else 'd'
    return f'{duration}{days_suffix}, {traffic_label}, {devices_label}{price_note}'


def _format_core_summary(lang_code: str, pricing_currency: str) -> str:
    base_price = _format_price_for_currency(settings.BASE_SUBSCRIPTION_PRICE, pricing_currency)
    device_limit = settings.DEFAULT_DEVICE_LIMIT
    traffic_limit = settings.DEFAULT_TRAFFIC_LIMIT_GB
    mode = settings.TRAFFIC_SELECTION_MODE.lower()
    if mode == 'fixed':
        traffic_mode = '⚙️ fixed'
    elif mode == 'fixed_with_topup':
        traffic_mode = '⚙️ fixed+topup'
    else:
        traffic_mode = '⚙️ selectable'
    traffic_label = _format_traffic_label(traffic_limit, lang_code, short=True)
    return f'{base_price}, {device_limit}📱, {traffic_label}, {traffic_mode}'


def _get_period_items(lang_code: str, period_price_map: dict[int, int] | None = None) -> list[PriceItem]:
    from app.config import PERIOD_PRICES

    items: list[PriceItem] = []
    for days in settings.get_available_subscription_periods():
        key = f'PRICE_{days}_DAYS'
        if period_price_map and days in period_price_map:
            price = period_price_map[days]
        else:
            price = PERIOD_PRICES.get(days, 0)
        items.append((key, _format_period_label(days, lang_code), price))
    return items


def _get_traffic_items(lang_code: str, traffic_price_map: dict[int, int] | None = None) -> list[PriceItem]:
    packages = _collect_traffic_packages()

    items: list[PriceItem] = []
    for package in packages:
        field = package.get('field')
        if not field:
            continue

        label = _format_traffic_label(package['gb'], lang_code)
        icon = '✅' if package['enabled'] else '⚪️'
        price = int(traffic_price_map.get(package['gb'], package['price'])) if traffic_price_map else int(package['price'])
        items.append((field, f'{icon} {label}', price))
    return items


def _get_extra_items(lang_code: str) -> list[PriceItem]:
    items: list[PriceItem] = []

    if hasattr(settings, 'PRICE_PER_DEVICE'):
        label = 'Дополнительное устройство' if lang_code == 'ru' else 'Extra device'
        items.append(('PRICE_PER_DEVICE', label, settings.PRICE_PER_DEVICE))

    return items


def _build_period_summary(items: Iterable[PriceItem], lang_code: str, fallback: str, pricing_currency: str) -> str:
    parts: list[str] = []
    for key, label, price in items:
        try:
            days = int(key.replace('PRICE_', '').replace('_DAYS', ''))
        except ValueError:
            days = None

        if days is not None:
            suffix = 'д' if lang_code == 'ru' else 'd'
            short_label = f'{days}{suffix}'
        else:
            short_label = label

        parts.append(f'{short_label}: {_format_price_for_currency(price, pricing_currency)}')

    return ', '.join(parts) if parts else fallback


def _build_traffic_summary(
    lang_code: str,
    fallback: str,
    pricing_currency: str,
    traffic_price_map: dict[int, int] | None = None,
) -> str:
    packages = _collect_traffic_packages()
    enabled_packages = [package for package in packages if package['enabled']]

    if not enabled_packages:
        return fallback

    parts: list[str] = []
    for package in enabled_packages:
        short_label = _format_traffic_label(package['gb'], lang_code, short=True)
        price = int(traffic_price_map.get(package['gb'], package['price'])) if traffic_price_map else int(package['price'])
        parts.append(f'{short_label}: {_format_price_for_currency(price, pricing_currency)}')

    return ', '.join(parts) if parts else fallback


def _build_period_options_summary(lang_code: str) -> str:
    suffix = 'д' if lang_code == 'ru' else 'd'
    available = ', '.join(f'{days}{suffix}' for days in settings.get_available_subscription_periods())
    renewal = ', '.join(f'{days}{suffix}' for days in settings.get_available_renewal_periods())
    if lang_code == 'ru':
        return f'Подписки: {available or "—"} | Продления: {renewal or "—"}'
    return f'Subscriptions: {available or "-"} | Renewals: {renewal or "-"}'


def _build_extra_summary(items: Iterable[PriceItem], fallback: str, pricing_currency: str) -> str:
    parts = [f'{label}: {_format_price_for_currency(price, pricing_currency)}' for key, label, price in items]
    return ', '.join(parts) if parts else fallback


def _build_settings_section(
    section: str,
    language: str,
    pricing_currency: str,
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    lang_code = _language_code(language)
    entries = SETTING_ENTRIES_BY_SECTION.get(section, ())
    normalized_currency = _normalize_pricing_currency(pricing_currency)

    if section == 'trial':
        title = texts.t('ADMIN_PRICING_SECTION_TRIAL_TITLE', '🎁 Пробный период')
    elif section == 'core':
        title = texts.t('ADMIN_PRICING_SECTION_CORE_TITLE', '⚙️ Настройки тарифов')
    else:
        title = texts.t('ADMIN_PRICING_SECTION_SETTINGS_GENERIC', '⚙️ Настройки')

    lines: list[str] = [title, '']
    lines.append(f'🌍 {texts.t("ADMIN_PRICING_ACTIVE_CURRENCY", "Активная валюта цен")}: <b>{normalized_currency}</b>')
    lines.append('')
    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    if entries:
        lines.append(
            texts.t(
                'ADMIN_PRICING_SECTION_CURRENT',
                'Текущие значения:',
            )
        )
        lines.append('')

    for entry in entries:
        label = entry.label(lang_code)
        value = bot_configuration_service.get_current_value(entry.key)
        formatted = bot_configuration_service.format_value_human(entry.key, value)

        if entry.action == 'toggle':
            state_icon = '✅' if bool(value) else '⚪️'
            lines.append(f'{state_icon} <b>{label}</b> — {formatted}')
            button_text = texts.t(
                'ADMIN_PRICING_SETTING_TOGGLE_STATEFUL',
                '{icon} {label}',
            ).format(icon=state_icon, label=label)
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=button_text,
                        callback_data=(f'admin_pricing_toggle:{section}:{_encode_setting_callback_key(entry.key)}'),
                    )
                ]
            )
        elif entry.action == 'choice' and entry.choices:
            lines.append(f'• <b>{label}</b>: {formatted}')
            buttons: list[types.InlineKeyboardButton] = []
            for option in entry.choices:
                is_active = value == option.value
                icon = '✅' if is_active else '⚪️'
                buttons.append(
                    types.InlineKeyboardButton(
                        text=f'{icon} {option.label(lang_code)}',
                        callback_data=(
                            f'admin_pricing_choice:{section}:{_encode_setting_callback_key(entry.key)}:{option.value}'
                        ),
                    )
                )
            for i in range(0, len(buttons), 2):
                keyboard_rows.append(buttons[i : i + 2])
        else:
            lines.append(f'• <b>{label}</b>: {formatted}')
            button_text = texts.t(
                'ADMIN_PRICING_SETTING_EDIT_WITH_VALUE',
                '✏️ {label} • {value}',
            ).format(label=label, value=formatted)
            keyboard_rows.append(
                [
                    types.InlineKeyboardButton(
                        text=button_text,
                        callback_data=(f'admin_pricing_setting:{section}:{_encode_setting_callback_key(entry.key)}'),
                    )
                ]
            )

        description = entry.description(lang_code)
        if description:
            lines.append(f'<i>{description}</i>')
        lines.append('')

    if entries:
        lines.append(texts.t('ADMIN_PRICING_SECTION_PROMPT', 'Выберите что изменить:'))
    else:
        lines.append(texts.t('ADMIN_PRICING_SECTION_EMPTY', 'Нет параметров для изменения.'))

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRICING_BUTTON_CURRENCY',
                    '🌍 Валюта: {currency}',
                ).format(currency=normalized_currency),
                callback_data='admin_pricing_pick_currency',
            )
        ]
    )
    keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_pricing')])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return '\n'.join(lines).strip(), keyboard


def _build_traffic_options_section(
    language: str,
    pricing_currency: str,
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    lang_code = _language_code(language)
    packages = _collect_traffic_packages()
    normalized_currency = _normalize_pricing_currency(pricing_currency)

    title = texts.t(
        'ADMIN_PRICING_SECTION_TRAFFIC_OPTIONS_TITLE',
        '🚦 Отображение пакетов трафика',
    )

    lines: list[str] = [title, '']

    enabled_labels = [
        _format_traffic_label(package['gb'], lang_code, short=True) for package in packages if package['enabled']
    ]

    if enabled_labels:
        lines.append(
            texts.t(
                'ADMIN_PRICING_SECTION_TRAFFIC_OPTIONS_ACTIVE',
                'Активные пакеты: {items}',
            ).format(items=', '.join(enabled_labels))
        )
    else:
        lines.append(
            texts.t(
                'ADMIN_PRICING_SECTION_TRAFFIC_OPTIONS_NONE',
                'Активных пакетов нет.',
            )
        )

    lines.append('')
    lines.append(f'🌍 {texts.t("ADMIN_PRICING_ACTIVE_CURRENCY", "Активная валюта цен")}: <b>{normalized_currency}</b>')
    lines.append('')
    lines.append(
        texts.t(
            'ADMIN_PRICING_SECTION_TRAFFIC_OPTIONS_PROMPT',
            'Нажмите на пакет, чтобы включить или выключить его отображение.',
        )
    )

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []
    buttons: list[types.InlineKeyboardButton] = []

    for package in packages:
        icon = '✅' if package['enabled'] else '⚪️'
        label = _format_traffic_label(package['gb'], lang_code, short=True)
        buttons.append(
            types.InlineKeyboardButton(
                text=f'{icon} {label}',
                callback_data=f'admin_pricing_toggle_traffic:{package["gb"]}',
            )
        )

    for i in range(0, len(buttons), 3):
        keyboard_rows.append(buttons[i : i + 3])

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRICING_BUTTON_CURRENCY',
                    '🌍 Валюта: {currency}',
                ).format(currency=normalized_currency),
                callback_data='admin_pricing_pick_currency',
            )
        ]
    )
    keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_pricing')])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return '\n'.join(lines), keyboard


def _build_period_options_section(
    language: str,
    pricing_currency: str,
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    lang_code = _language_code(language)
    normalized_currency = _normalize_pricing_currency(pricing_currency)
    suffix = 'д' if lang_code == 'ru' else 'd'

    # Используем методы без фильтрации по ценам для админки
    available_subscription = set(settings.get_configured_subscription_periods())
    available_renewal = set(settings.get_configured_renewal_periods())

    subscription_options = (14, 30, 60, 90, 180, 360)
    renewal_options = (30, 60, 90, 180, 360)

    title = texts.t('ADMIN_PRICING_SECTION_PERIOD_OPTIONS_TITLE', '🗓 Доступные периоды')
    lines: list[str] = [title, '']

    sub_list = ', '.join(f'{days}{suffix}' for days in sorted(available_subscription)) or '—'
    renew_list = ', '.join(f'{days}{suffix}' for days in sorted(available_renewal)) or '—'

    lines.append(
        texts.t(
            'ADMIN_PRICING_SECTION_PERIOD_OPTIONS_SUB',
            'Активные периоды подписки: {items}',
        ).format(items=sub_list)
    )
    lines.append(
        texts.t(
            'ADMIN_PRICING_SECTION_PERIOD_OPTIONS_RENEW',
            'Активные периоды продления: {items}',
        ).format(items=renew_list)
    )
    lines.append('')
    lines.append(
        texts.t(
            'ADMIN_PRICING_SECTION_PERIOD_OPTIONS_PROMPT',
            'Нажмите на период, чтобы включить или выключить его отображение.',
        )
    )
    lines.append('')
    lines.append(f'🌍 {texts.t("ADMIN_PRICING_ACTIVE_CURRENCY", "Активная валюта цен")}: <b>{normalized_currency}</b>')

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    sub_buttons = []
    for days in subscription_options:
        icon = '✅' if days in available_subscription else '⚪️'
        sub_buttons.append(
            types.InlineKeyboardButton(
                text=f'{icon} {days}{suffix}',
                callback_data=f'admin_pricing_toggle_period:subscription:{days}',
            )
        )
    for i in range(0, len(sub_buttons), 3):
        keyboard_rows.append(sub_buttons[i : i + 3])

    renew_buttons = []
    for days in renewal_options:
        icon = '✅' if days in available_renewal else '⚪️'
        renew_buttons.append(
            types.InlineKeyboardButton(
                text=f'{icon} {days}{suffix}',
                callback_data=f'admin_pricing_toggle_period:renewal:{days}',
            )
        )
    for i in range(0, len(renew_buttons), 3):
        keyboard_rows.append(renew_buttons[i : i + 3])

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRICING_BUTTON_CURRENCY',
                    '🌍 Валюта: {currency}',
                ).format(currency=normalized_currency),
                callback_data='admin_pricing_pick_currency',
            )
        ]
    )
    keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_pricing')])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return '\n'.join(lines), keyboard


def _build_overview(
    language: str,
    pricing_currency: str,
    period_price_map: dict[int, int] | None = None,
    traffic_price_map: dict[int, int] | None = None,
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    lang_code = _language_code(language)
    normalized_currency = _normalize_pricing_currency(pricing_currency)

    period_items = _get_period_items(lang_code, period_price_map)
    _get_traffic_items(lang_code, traffic_price_map)
    extra_items = _get_extra_items(lang_code)

    fallback = texts.t('ADMIN_PRICING_SUMMARY_EMPTY', '—')
    summary_periods = _build_period_summary(period_items, lang_code, fallback, normalized_currency)
    summary_traffic = _build_traffic_summary(
        lang_code,
        fallback,
        normalized_currency,
        traffic_price_map,
    )
    summary_extra = _build_extra_summary(extra_items, fallback, normalized_currency)
    summary_trial = _format_trial_summary(lang_code, normalized_currency)
    summary_core = _format_core_summary(lang_code, normalized_currency)
    summary_period_options = _build_period_options_summary(lang_code)

    lines = [
        f'💰 <b>{texts.t("ADMIN_PRICING_MENU_TITLE", "Управление ценами")}</b>',
        texts.t(
            'ADMIN_PRICING_MENU_DESCRIPTION',
            'Быстрый доступ к настройкам тарифов, периодов и пакетов.',
        ),
        '',
        f'🌍 {texts.t("ADMIN_PRICING_ACTIVE_CURRENCY", "Активная валюта цен")}: <b>{normalized_currency}</b>',
        '',
        f'<b>{texts.t("ADMIN_PRICING_MENU_SUMMARY", "Краткая сводка")}</b>',
        f'🎁 {texts.t("ADMIN_PRICING_MENU_SUMMARY_TRIAL", "Триал: {summary}").format(summary=summary_trial)}',
        f'⚙️ {texts.t("ADMIN_PRICING_MENU_SUMMARY_CORE", "Базовые лимиты: {summary}").format(summary=summary_core)}',
        f'🗓 {texts.t("ADMIN_PRICING_MENU_SUMMARY_PERIOD_OPTIONS", "Доступные периоды: {summary}").format(summary=summary_period_options)}',
        f'💵 {texts.t("ADMIN_PRICING_MENU_SUMMARY_PERIODS", "Стоимость периодов: {summary}").format(summary=summary_periods)}',
        f'📦 {texts.t("ADMIN_PRICING_MENU_SUMMARY_TRAFFIC", "Пакеты трафика: {summary}").format(summary=summary_traffic)}',
        f'➕ {texts.t("ADMIN_PRICING_MENU_SUMMARY_EXTRA", "Дополнительно: {summary}").format(summary=summary_extra)}',
        '',
        texts.t('ADMIN_PRICING_MENU_PROMPT', 'Выберите раздел для редактирования:'),
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRICING_BUTTON_CURRENCY',
                        '🌍 Валюта: {currency}',
                    ).format(currency=normalized_currency),
                    callback_data='admin_pricing_pick_currency',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_TRIAL', '🎁 Пробный период'),
                    callback_data='admin_pricing_section:trial',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_CORE', '⚙️ Настройки тарифов'),
                    callback_data='admin_pricing_section:core',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_PERIOD_OPTIONS', '🗓 Доступные периоды'),
                    callback_data='admin_pricing_section:period_options',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_PERIODS', '💵 Стоимость периодов'),
                    callback_data='admin_pricing_section:periods',
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_TRAFFIC', '📦 Пакеты трафика'),
                    callback_data='admin_pricing_section:traffic',
                ),
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRICING_BUTTON_TRAFFIC_OPTIONS',
                        '🚦 Отображение пакетов',
                    ),
                    callback_data='admin_pricing_section:traffic_options',
                ),
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_BUTTON_EXTRA', '➕ Дополнительно'),
                    callback_data='admin_pricing_section:extra',
                ),
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )

    return '\n'.join(lines), keyboard


def _build_section(
    section: str,
    language: str,
    pricing_currency: str,
    period_price_map: dict[int, int] | None = None,
    traffic_price_map: dict[int, int] | None = None,
) -> tuple[str, types.InlineKeyboardMarkup]:
    texts = get_texts(language)
    lang_code = _language_code(language)
    normalized_currency = _normalize_pricing_currency(pricing_currency)

    if section == 'periods':
        items = _get_period_items(lang_code, period_price_map)
        title = texts.t('ADMIN_PRICING_SECTION_PERIODS_TITLE', '🗓 Периоды подписки')
    elif section == 'traffic':
        items = _get_traffic_items(lang_code, traffic_price_map)
        title = texts.t('ADMIN_PRICING_SECTION_TRAFFIC_TITLE', '📦 Пакеты трафика')
    elif section == 'extra':
        items = _get_extra_items(lang_code)
        title = texts.t('ADMIN_PRICING_SECTION_EXTRA_TITLE', '➕ Дополнительные опции')
    elif section == 'traffic_options':
        return _build_traffic_options_section(language, normalized_currency)
    elif section in SETTING_ENTRIES_BY_SECTION:
        return _build_settings_section(section, language, normalized_currency)
    elif section == 'period_options':
        return _build_period_options_section(language, normalized_currency)
    else:
        items = _get_extra_items(lang_code)
        title = texts.t('ADMIN_PRICING_SECTION_EXTRA_TITLE', '➕ Дополнительные опции')

    lines = [title, '']
    lines.append(f'🌍 {texts.t("ADMIN_PRICING_ACTIVE_CURRENCY", "Активная валюта цен")}: <b>{normalized_currency}</b>')
    lines.append('')

    if items:
        for key, label, price in items:
            lines.append(f'• {label} — {_format_price_for_currency(price, normalized_currency)}')
        lines.append('')
        lines.append(texts.t('ADMIN_PRICING_SECTION_PROMPT', 'Выберите что изменить:'))
    else:
        lines.append(texts.t('ADMIN_PRICING_SECTION_EMPTY', 'Нет доступных значений.'))

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []
    for key, label, price in items:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f'{label} • {_format_price_for_currency(price, normalized_currency)}',
                    callback_data=f'admin_pricing_edit:{section}:{key}',
                )
            ]
        )

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRICING_BUTTON_CURRENCY',
                    '🌍 Валюта: {currency}',
                ).format(currency=normalized_currency),
                callback_data='admin_pricing_pick_currency',
            )
        ]
    )
    keyboard_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_pricing')])

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return '\n'.join(lines), keyboard


def _build_price_prompt(texts: Any, label: str, current_price: str, currency: str) -> str:
    normalized_currency = _normalize_pricing_currency(currency)
    if normalized_currency == 'RUB':
        input_hint = texts.t(
            'ADMIN_PRICING_EDIT_PROMPT',
            'Введите новую стоимость в рублях (например 990 или 990.50). Для бесплатного тарифа укажите 0.',
        )
    else:
        input_hint = texts.t(
            'ADMIN_PRICING_EDIT_PROMPT_CURRENCY',
            'Введите новую стоимость в валюте {currency} (например 990). Для бесплатного тарифа укажите 0.',
        ).format(currency=normalized_currency)

    lines = [
        f'💰 <b>{texts.t("ADMIN_PRICING_EDIT_TITLE", "Изменение цены")}</b>',
        '',
        f'{texts.t("ADMIN_PRICING_EDIT_TARGET", "Текущий тариф")}: <b>{label}</b>',
        f'{texts.t("ADMIN_PRICING_EDIT_CURRENCY", "Валюта")}: <b>{normalized_currency}</b>',
        f'{texts.t("ADMIN_PRICING_EDIT_CURRENT", "Текущее значение")}: <b>{current_price}</b>',
        '',
        input_hint,
        texts.t(
            'ADMIN_PRICING_EDIT_CANCEL_HINT',
            'Напишите «Отмена», чтобы вернуться без изменений.',
        ),
    ]
    return '\n'.join(lines)


async def _render_message(
    message: types.Message,
    text: str,
    keyboard: types.InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest as error:  # message changed elsewhere
        logger.debug('Failed to edit pricing message: %s', error)
        await message.answer(text, reply_markup=keyboard, parse_mode='HTML')


async def _render_message_by_id(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: types.InlineKeyboardMarkup,
) -> None:
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    except TelegramBadRequest as error:
        logger.debug('Failed to edit pricing message by id: %s', error)
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML')


def _parse_price_input(text: str, currency: str) -> int:
    normalized_currency = _normalize_pricing_currency(currency)
    meta = get_currency_meta(normalized_currency)
    normalized = (text or '').replace(' ', '').replace(',', '.').strip()
    upper = normalized.upper()
    for token in (normalized_currency.upper(), meta.symbol.upper()):
        if token:
            upper = upper.replace(token, '')
    upper = upper.replace('₽', '').replace('Р', '')
    normalized = upper.strip()
    if not normalized:
        raise ValueError('empty')

    try:
        value = Decimal(normalized)
    except InvalidOperation as error:
        raise ValueError('invalid') from error

    if value < 0:
        raise ValueError('negative')

    return major_to_minor(value, normalized_currency)


def _resolve_label(section: str, key: str, language: str) -> str:
    lang_code = _language_code(language)

    entry = SETTING_ENTRY_BY_KEY.get(key)
    if entry is not None:
        return entry.label(lang_code)

    if section == 'periods' and key.startswith('PRICE_') and key.endswith('_DAYS'):
        try:
            days = int(key.replace('PRICE_', '').replace('_DAYS', ''))
        except ValueError:
            days = None
        if days is not None:
            return _format_period_label(days, lang_code)

    if section == 'traffic' and key.startswith('PRICE_TRAFFIC_'):
        if key.endswith('UNLIMITED'):
            return _format_traffic_label(0, lang_code)
        digits = ''.join(ch for ch in key if ch.isdigit())
        try:
            gb = int(digits)
        except ValueError:
            gb = None
        if gb is not None:
            return _format_traffic_label(gb, lang_code)

    if key == 'PRICE_PER_DEVICE':
        return 'Дополнительное устройство' if lang_code == 'ru' else 'Extra device'

    return key


async def _render_section_for_currency(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
    section: str,
) -> None:
    data = await state.get_data()
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view=section,
    )
    period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
    text, keyboard = _build_section(
        section,
        db_user.language,
        pricing_currency,
        period_price_map=period_price_map,
        traffic_price_map=traffic_price_map,
    )
    await _render_message(callback.message, text, keyboard)


@admin_required
@error_handler
async def show_pricing_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    pricing_currency = _default_pricing_currency()
    await state.clear()
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view='overview',
    )

    period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
    text, keyboard = _build_overview(
        db_user.language,
        pricing_currency,
        period_price_map=period_price_map,
        traffic_price_map=traffic_price_map,
    )
    await _render_message(callback.message, text, keyboard)
    await callback.answer()


@admin_required
@error_handler
async def show_pricing_section(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    section = callback.data.split(':', 1)[1]
    existing = await state.get_data()
    pricing_currency = _normalize_pricing_currency(existing.get('pricing_currency'))
    await state.clear()
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view=section,
    )

    period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
    text, keyboard = _build_section(
        section,
        db_user.language,
        pricing_currency,
        period_price_map=period_price_map,
        traffic_price_map=traffic_price_map,
    )
    await _render_message(callback.message, text, keyboard)
    await callback.answer()


@admin_required
@error_handler
async def start_price_edit(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    _, section, key = callback.data.split(':', 2)
    texts = get_texts(db_user.language)
    label = _resolve_label(section, key, db_user.language)
    data = await state.get_data()
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    value_currency = _get_price_input_currency(key, pricing_currency)

    await state.update_data(
        pricing_key=key,
        pricing_section=section,
        pricing_message_id=callback.message.message_id,
        pricing_mode='price',
        pricing_currency=pricing_currency,
        pricing_value_currency=value_currency,
    )
    await state.set_state(PricingStates.waiting_for_value)

    current_minor = await _resolve_price_value_minor(db, key, pricing_currency)
    current_price = _format_price_for_currency(current_minor, value_currency)
    prompt = _build_price_prompt(texts, label, current_price, value_currency)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_EDIT_CANCEL', '❌ Отмена'),
                    callback_data=f'admin_pricing_section:{section}',
                )
            ]
        ]
    )

    await _render_message(callback.message, prompt, keyboard)
    await callback.answer()


@admin_required
@error_handler
async def start_setting_edit(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, section, raw_key = callback.data.split(':', 2)
    except ValueError:
        await callback.answer()
        return

    key = _decode_setting_callback_key(raw_key)
    entry = SETTING_ENTRY_BY_KEY.get(key)
    texts = get_texts(db_user.language)
    lang_code = _language_code(db_user.language)
    label = entry.label(lang_code) if entry else key
    current_value = bot_configuration_service.get_current_value(key)
    formatted_current = bot_configuration_service.format_value_human(key, current_value)
    guidance = bot_configuration_service.get_setting_guidance(key)

    mode = 'price' if entry and entry.action == 'price' else 'setting'
    existing = await state.get_data()
    pricing_currency = _normalize_pricing_currency(existing.get('pricing_currency'))
    value_currency = _get_price_input_currency(key, pricing_currency)

    await state.update_data(
        pricing_key=key,
        pricing_section=section,
        pricing_message_id=callback.message.message_id,
        pricing_mode=mode,
        pricing_label=label,
        pricing_currency=pricing_currency,
        pricing_value_currency=value_currency,
    )
    await state.set_state(PricingStates.waiting_for_value)

    if mode == 'price':
        prompt = _build_price_prompt(
            texts,
            label,
            _format_price_for_currency(int(current_value or 0), value_currency),
            value_currency,
        )
    else:
        description = guidance.get('description') or ''
        format_hint = guidance.get('format') or ''
        example = guidance.get('example') or '—'
        warning = guidance.get('warning') or ''
        prompt_parts = [
            f'⚙️ <b>{texts.t("ADMIN_PRICING_SETTING_EDIT_TITLE", "Настройка параметра")}</b>',
            '',
            f'{texts.t("ADMIN_PRICING_SETTING_PARAMETER", "Параметр")}: <b>{label}</b>',
            f'{texts.t("ADMIN_PRICING_SETTING_CURRENT", "Текущее значение")}: <b>{formatted_current}</b>',
        ]
        if description:
            prompt_parts.extend(['', description])
        prompt_parts.extend(
            [
                '',
                f'ℹ️ {texts.t("ADMIN_PRICING_SETTING_FORMAT", "Формат ввода")}: {format_hint}',
                f'📌 {texts.t("ADMIN_PRICING_SETTING_EXAMPLE", "Пример")}: {example}',
            ]
        )
        if warning:
            prompt_parts.append(f'⚠️ {texts.t("ADMIN_PRICING_SETTING_WARNING", "Важно")}: {warning}')
        prompt_parts.extend(
            [
                '',
                texts.t(
                    'ADMIN_PRICING_SETTING_PROMPT',
                    'Отправьте новое значение или напишите «Отмена». Для очистки используйте none.',
                ),
                texts.t(
                    'ADMIN_PRICING_SETTING_CANCEL_HINT',
                    'Чтобы вернуться без изменений, ответьте «Отмена».',
                ),
            ]
        )
        prompt = '\n'.join(prompt_parts)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRICING_EDIT_CANCEL', '❌ Отмена'),
                    callback_data=f'admin_pricing_section:{section}',
                )
            ]
        ]
    )

    await _render_message(callback.message, prompt, keyboard)
    await callback.answer()


async def process_pricing_input(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
) -> None:
    data = await state.get_data()
    key = data.get('pricing_key')
    section = data.get('pricing_section', 'periods')
    message_id = data.get('pricing_message_id')
    mode = data.get('pricing_mode', 'price')
    stored_label = data.get('pricing_label')
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    raw_value_currency = data.get('pricing_value_currency')
    value_currency = _normalize_pricing_currency(raw_value_currency) if raw_value_currency else None

    texts = get_texts(db_user.language)

    if not key:
        await message.answer(texts.t('ADMIN_PRICING_EDIT_EXPIRED', 'Сессия редактирования истекла.'))
        await state.clear()
        return

    raw_value = message.text or ''
    if raw_value.strip().lower() in {'cancel', 'отмена'}:
        await state.clear()
        await state.update_data(
            pricing_currency=pricing_currency,
            pricing_current_view=section,
        )
        period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
        section_text, section_keyboard = _build_section(
            section,
            db_user.language,
            pricing_currency,
            period_price_map=period_price_map,
            traffic_price_map=traffic_price_map,
        )
        if message_id:
            await _render_message_by_id(
                message.bot,
                message.chat.id,
                message_id,
                section_text,
                section_keyboard,
            )
        await message.answer(texts.t('ADMIN_PRICING_EDIT_CANCELLED', 'Изменения отменены.'))
        return

    if mode == 'price':
        try:
            parse_currency = value_currency or _get_price_input_currency(key, pricing_currency)
            new_value = _parse_price_input(raw_value, parse_currency)
        except ValueError:
            await message.answer(
                texts.t(
                    'ADMIN_PRICING_EDIT_INVALID',
                    'Не удалось распознать цену. Укажите число (например 990 или 990.50).',
                )
            )
            return
    else:
        try:
            new_value = bot_configuration_service.parse_user_value(key, raw_value)
        except ValueError as error:
            error_text = str(error) or texts.t(
                'ADMIN_PRICING_SETTING_INVALID',
                'Не удалось обновить параметр. Проверьте формат значения.',
            )
            await message.answer(error_text)
            return

    if mode == 'price' and settings.MULTI_CURRENCY_ENABLED and _is_currency_table_price_key(key):
        await _upsert_currency_price(db, key, pricing_currency, int(new_value))
        await db.commit()
    else:
        await bot_configuration_service.set_value(db, key, new_value)
        await db.commit()

        if key.startswith('PRICE_TRAFFIC_'):
            packages = _collect_traffic_packages()
            await _save_traffic_packages(db, packages, skip_if_same=True)

    period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
    section_text, section_keyboard = _build_section(
        section,
        db_user.language,
        pricing_currency,
        period_price_map=period_price_map,
        traffic_price_map=traffic_price_map,
    )

    if mode == 'price':
        if message_id:
            await _render_message_by_id(
                message.bot,
                message.chat.id,
                message_id,
                section_text,
                section_keyboard,
            )
        try:
            await message.delete()
        except TelegramBadRequest as error:
            logger.debug('Failed to delete pricing input message: %s', error)
        await state.clear()
        await state.update_data(
            pricing_currency=pricing_currency,
            pricing_current_view=section,
        )
        return
    entry = SETTING_ENTRY_BY_KEY.get(key)
    lang_code = _language_code(db_user.language)
    label = entry.label(lang_code) if entry else (stored_label or key)
    formatted_value = bot_configuration_service.format_value_human(
        key, bot_configuration_service.get_current_value(key)
    )
    await message.answer(
        texts.t(
            'ADMIN_PRICING_SETTING_SUCCESS',
            'Параметр {label} обновлен: {value}',
        ).format(label=label, value=formatted_value)
    )

    await state.clear()
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view=section,
    )

    if message_id:
        period_price_map, traffic_price_map = await _load_currency_price_maps(db, pricing_currency)
        section_text, section_keyboard = _build_section(
            section,
            db_user.language,
            pricing_currency,
            period_price_map=period_price_map,
            traffic_price_map=traffic_price_map,
        )
        await _render_message_by_id(
            message.bot,
            message.chat.id,
            message_id,
            section_text,
            section_keyboard,
        )


@admin_required
@error_handler
async def toggle_setting(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, section, raw_key = callback.data.split(':', 2)
    except ValueError:
        await callback.answer()
        return

    key = _decode_setting_callback_key(raw_key)
    entry = SETTING_ENTRY_BY_KEY.get(key)
    if not entry or entry.action != 'toggle':
        await callback.answer()
        return

    current = bool(bot_configuration_service.get_current_value(key))
    new_value = not current
    await bot_configuration_service.set_value(db, key, new_value)
    await db.commit()

    value_text = bot_configuration_service.format_value_human(key, new_value)
    await callback.answer(value_text, show_alert=False)

    await _render_section_for_currency(callback, state, db_user, db, section)


@admin_required
@error_handler
async def select_setting_choice(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, section, raw_key, value_raw = callback.data.split(':', 3)
    except ValueError:
        await callback.answer()
        return

    key = _decode_setting_callback_key(raw_key)
    entry = SETTING_ENTRY_BY_KEY.get(key)
    if not entry or entry.action != 'choice' or not entry.choices:
        await callback.answer()
        return

    target_option = None
    for option in entry.choices:
        if str(option.value) == value_raw:
            target_option = option
            break

    if target_option is None:
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    current_value = bot_configuration_service.get_current_value(key)
    if current_value == target_option.value:
        await callback.answer(
            texts.t(
                'ADMIN_PRICING_CHOICE_ALREADY',
                'Это значение уже активно.',
            )
        )
        return

    await bot_configuration_service.set_value(db, key, target_option.value)
    await db.commit()

    lang_code = _language_code(db_user.language)
    await callback.answer(
        texts.t(
            'ADMIN_PRICING_CHOICE_UPDATED',
            'Выбрано: {label}',
        ).format(label=target_option.label(lang_code))
    )

    await _render_section_for_currency(callback, state, db_user, db, section)


@admin_required
@error_handler
async def toggle_traffic_package(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, gb_raw = callback.data.split(':', 1)
        gb_value = int(gb_raw)
    except (ValueError, TypeError):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    packages = _collect_traffic_packages()

    target_index = next((index for index, pkg in enumerate(packages) if pkg['gb'] == gb_value), None)
    if target_index is None:
        await callback.answer()
        return

    enabled_count = sum(1 for pkg in packages if pkg['enabled'])
    target_package = packages[target_index]

    if target_package['enabled'] and enabled_count <= 1:
        await callback.answer(
            texts.t(
                'ADMIN_PRICING_TRAFFIC_PACKAGE_MIN',
                'Должен оставаться хотя бы один пакет.',
            ),
            show_alert=True,
        )
        return

    target_package['enabled'] = not target_package['enabled']

    await _save_traffic_packages(db, packages)

    status_text = (
        texts.t('ADMIN_PRICING_TRAFFIC_PACKAGE_ENABLED', 'Пакет включен.')
        if target_package['enabled']
        else texts.t('ADMIN_PRICING_TRAFFIC_PACKAGE_DISABLED', 'Пакет отключен.')
    )
    await callback.answer(status_text)

    data = await state.get_data()
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view='traffic_options',
    )
    text, keyboard = _build_traffic_options_section(db_user.language, pricing_currency)
    await _render_message(callback.message, text, keyboard)


@admin_required
@error_handler
async def toggle_period_option(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, target, value_raw = callback.data.split(':', 2)
        days = int(value_raw)
    except (ValueError, TypeError):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    if target == 'subscription':
        # Используем метод без фильтрации по ценам для админки
        current = set(settings.get_configured_subscription_periods())
        options = {14, 30, 60, 90, 180, 360}
        setting_key = 'AVAILABLE_SUBSCRIPTION_PERIODS'
    elif target == 'renewal':
        # Используем метод без фильтрации по ценам для админки
        current = set(settings.get_configured_renewal_periods())
        options = {30, 60, 90, 180, 360}
        setting_key = 'AVAILABLE_RENEWAL_PERIODS'
    else:
        await callback.answer()
        return

    if days not in options:
        await callback.answer()
        return

    if days in current:
        if len(current) == 1:
            await callback.answer(
                texts.t(
                    'ADMIN_PRICING_PERIOD_MIN',
                    'Должен оставаться хотя бы один период.',
                ),
                show_alert=True,
            )
            return
        current.remove(days)
        action_text = texts.t('ADMIN_PRICING_PERIOD_DISABLED', 'Период отключен.')
    else:
        current.add(days)
        action_text = texts.t('ADMIN_PRICING_PERIOD_ENABLED', 'Период включен.')

    new_value = ','.join(str(item) for item in sorted(current))
    await bot_configuration_service.set_value(db, setting_key, new_value)
    await db.commit()

    await callback.answer(action_text)

    data = await state.get_data()
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    await state.update_data(
        pricing_currency=pricing_currency,
        pricing_current_view='period_options',
    )
    text, keyboard = _build_period_options_section(db_user.language, pricing_currency)
    await _render_message(callback.message, text, keyboard)


@admin_required
@error_handler
async def show_currency_picker(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    data = await state.get_data()
    pricing_currency = _normalize_pricing_currency(data.get('pricing_currency'))
    current_view = data.get('pricing_current_view', 'overview')
    supported = _parse_supported_balance_currencies()

    buttons: list[types.InlineKeyboardButton] = []
    for code in supported:
        icon = '✅' if code == pricing_currency else '⚪️'
        buttons.append(
            types.InlineKeyboardButton(
                text=f'{icon} {code}',
                callback_data=f'admin_pricing_set_currency:{code}',
            )
        )

    keyboard_rows: list[list[types.InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 3):
        keyboard_rows.append(buttons[i : i + 3])

    back_callback = 'admin_pricing'
    if isinstance(current_view, str) and current_view and current_view != 'overview':
        back_callback = f'admin_pricing_section:{current_view}'

    keyboard_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.BACK,
                callback_data=back_callback,
            )
        ]
    )

    text = '\n'.join(
        [
            f'🌍 <b>{texts.t("ADMIN_PRICING_CURRENCY_PICKER_TITLE", "Выбор валюты для цен")}</b>',
            '',
            texts.t(
                'ADMIN_PRICING_CURRENCY_PICKER_DESC',
                'Выберите валюту, в которой хотите редактировать цены периодов и трафика.',
            ),
            f'{texts.t("ADMIN_PRICING_CURRENCY_PICKER_CURRENT", "Сейчас")}: <b>{pricing_currency}</b>',
        ]
    )

    await _render_message(callback.message, text, types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()


@admin_required
@error_handler
async def set_pricing_currency(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    try:
        _, currency_raw = callback.data.split(':', 1)
    except ValueError:
        await callback.answer()
        return

    selected_currency = _normalize_pricing_currency(currency_raw)
    data = await state.get_data()
    current_view = data.get('pricing_current_view', 'overview')
    await state.update_data(pricing_currency=selected_currency)

    period_price_map, traffic_price_map = await _load_currency_price_maps(db, selected_currency)
    if isinstance(current_view, str) and current_view and current_view != 'overview':
        await state.update_data(pricing_current_view=current_view)
        text, keyboard = _build_section(
            current_view,
            db_user.language,
            selected_currency,
            period_price_map=period_price_map,
            traffic_price_map=traffic_price_map,
        )
    else:
        await state.update_data(pricing_current_view='overview')
        text, keyboard = _build_overview(
            db_user.language,
            selected_currency,
            period_price_map=period_price_map,
            traffic_price_map=traffic_price_map,
        )

    await _render_message(callback.message, text, keyboard)
    await callback.answer(
        get_texts(db_user.language)
        .t('ADMIN_PRICING_CURRENCY_PICKER_SELECTED', 'Выбрана валюта: {currency}')
        .format(currency=selected_currency)
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        show_pricing_menu,
        F.data.in_({'admin_pricing', 'admin_subs_pricing'}),
    )
    dp.callback_query.register(
        show_pricing_section,
        F.data.startswith('admin_pricing_section:'),
    )
    dp.callback_query.register(
        show_currency_picker,
        F.data == 'admin_pricing_pick_currency',
    )
    dp.callback_query.register(
        set_pricing_currency,
        F.data.startswith('admin_pricing_set_currency:'),
    )
    dp.callback_query.register(
        start_price_edit,
        F.data.startswith('admin_pricing_edit:'),
    )
    dp.callback_query.register(
        start_setting_edit,
        F.data.startswith('admin_pricing_setting:'),
    )
    dp.callback_query.register(
        toggle_setting,
        F.data.startswith('admin_pricing_toggle:'),
    )
    dp.callback_query.register(
        select_setting_choice,
        F.data.startswith('admin_pricing_choice:'),
    )
    dp.callback_query.register(
        toggle_traffic_package,
        F.data.startswith('admin_pricing_toggle_traffic:'),
    )
    dp.callback_query.register(
        toggle_period_option,
        F.data.startswith('admin_pricing_toggle_period:'),
    )
    dp.message.register(
        process_pricing_input,
        PricingStates.waiting_for_value,
    )
