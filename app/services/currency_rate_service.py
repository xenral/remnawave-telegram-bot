from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CurrencyRate
from app.utils.money import convert_minor_with_rate, normalize_currency


class CurrencyRateService:
    """Admin-managed FX rates and minor-unit conversion helpers."""

    @staticmethod
    def _normalize_pair(from_currency: str | None, to_currency: str | None) -> tuple[str, str]:
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        return source, target

    @classmethod
    def _fixed_rate(cls, from_currency: str, to_currency: str) -> Decimal | None:
        # TMN is display alias over IRR.
        if from_currency == to_currency:
            return Decimal('1')
        if from_currency == 'IRR' and to_currency == 'TMN':
            return Decimal('0.1')
        if from_currency == 'TMN' and to_currency == 'IRR':
            return Decimal('10')
        return None

    @classmethod
    async def get_rate(
        cls,
        db: AsyncSession,
        *,
        from_currency: str | None,
        to_currency: str | None,
    ) -> Decimal:
        source, target = cls._normalize_pair(from_currency, to_currency)

        fixed = cls._fixed_rate(source, target)
        if fixed is not None:
            return fixed

        direct = await db.execute(
            select(CurrencyRate).where(
                CurrencyRate.from_currency == source,
                CurrencyRate.to_currency == target,
                CurrencyRate.is_active.is_(True),
            )
        )
        row = direct.scalar_one_or_none()
        if row and row.rate > 0:
            return Decimal(str(row.rate))

        reverse = await db.execute(
            select(CurrencyRate).where(
                CurrencyRate.from_currency == target,
                CurrencyRate.to_currency == source,
                CurrencyRate.is_active.is_(True),
            )
        )
        reverse_row = reverse.scalar_one_or_none()
        if reverse_row and reverse_row.rate > 0:
            return Decimal('1') / Decimal(str(reverse_row.rate))

        raise ValueError(f'Currency rate not configured: {source} -> {target}')

    @classmethod
    async def convert_minor(
        cls,
        db: AsyncSession,
        *,
        amount_minor: int,
        from_currency: str | None,
        to_currency: str | None,
    ) -> int:
        source, target = cls._normalize_pair(from_currency, to_currency)
        if source == target:
            return amount_minor
        rate = await cls.get_rate(db, from_currency=source, to_currency=target)
        return convert_minor_with_rate(
            amount_minor,
            from_currency=source,
            to_currency=target,
            rate=rate,
        )


currency_rate_service = CurrencyRateService()
