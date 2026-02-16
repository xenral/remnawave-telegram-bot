"""Admin routes for currency settings and manual FX rates."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CurrencyRate, SubscriptionPeriodPrice, TrafficPackagePrice, User
from app.utils.money import normalize_currency

from ..dependencies import get_cabinet_db, get_current_admin_user


router = APIRouter(prefix='/admin/currencies', tags=['Cabinet Admin Currencies'])


class CurrencyRateResponse(BaseModel):
    from_currency: str
    to_currency: str
    rate: float
    is_active: bool
    updated_by_user_id: int | None = None
    updated_at: datetime | None = None


class CurrencyRateUpsertRequest(BaseModel):
    rate: float = Field(..., gt=0)
    is_active: bool = True


class SubscriptionPeriodPriceResponse(BaseModel):
    period_days: int
    currency: str
    amount_minor: int
    is_active: bool


class SubscriptionPeriodPriceUpsertRequest(BaseModel):
    amount_minor: int = Field(..., ge=0)
    is_active: bool = True


class TrafficPackagePriceResponse(BaseModel):
    package_gb: int
    currency: str
    amount_minor: int
    is_active: bool


class TrafficPackagePriceUpsertRequest(BaseModel):
    amount_minor: int = Field(..., ge=0)
    is_active: bool = True


@router.get('/rates', response_model=list[CurrencyRateResponse])
async def list_currency_rates(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    result = await db.execute(select(CurrencyRate).order_by(CurrencyRate.from_currency, CurrencyRate.to_currency))
    rows = list(result.scalars().all())
    return [
        CurrencyRateResponse(
            from_currency=row.from_currency,
            to_currency=row.to_currency,
            rate=row.rate,
            is_active=row.is_active,
            updated_by_user_id=row.updated_by_user_id,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.put('/rates/{from_currency}/{to_currency}', response_model=CurrencyRateResponse)
async def upsert_currency_rate(
    from_currency: str,
    to_currency: str,
    payload: CurrencyRateUpsertRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = normalize_currency(from_currency)
    target = normalize_currency(to_currency)
    if source == target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='from_currency must differ from to_currency')

    result = await db.execute(
        select(CurrencyRate).where(CurrencyRate.from_currency == source, CurrencyRate.to_currency == target)
    )
    row = result.scalar_one_or_none()
    if row:
        row.rate = payload.rate
        row.is_active = payload.is_active
        row.updated_by_user_id = admin.id
    else:
        row = CurrencyRate(
            from_currency=source,
            to_currency=target,
            rate=payload.rate,
            is_active=payload.is_active,
            updated_by_user_id=admin.id,
        )
        db.add(row)

    await db.commit()
    await db.refresh(row)
    return CurrencyRateResponse(
        from_currency=row.from_currency,
        to_currency=row.to_currency,
        rate=row.rate,
        is_active=row.is_active,
        updated_by_user_id=row.updated_by_user_id,
        updated_at=row.updated_at,
    )


@router.delete('/rates/{from_currency}/{to_currency}')
async def delete_currency_rate(
    from_currency: str,
    to_currency: str,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = normalize_currency(from_currency)
    target = normalize_currency(to_currency)
    result = await db.execute(
        select(CurrencyRate).where(CurrencyRate.from_currency == source, CurrencyRate.to_currency == target)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Currency rate not found')
    await db.delete(row)
    await db.commit()
    return {'success': True}


@router.get('/subscription-period-prices', response_model=list[SubscriptionPeriodPriceResponse])
async def list_subscription_period_prices(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    result = await db.execute(
        select(SubscriptionPeriodPrice).order_by(
            SubscriptionPeriodPrice.period_days,
            SubscriptionPeriodPrice.currency,
        )
    )
    rows = list(result.scalars().all())
    return [
        SubscriptionPeriodPriceResponse(
            period_days=row.period_days,
            currency=row.currency,
            amount_minor=row.amount_minor,
            is_active=row.is_active,
        )
        for row in rows
    ]


@router.put('/subscription-period-prices/{period_days}/{currency}', response_model=SubscriptionPeriodPriceResponse)
async def upsert_subscription_period_price(
    period_days: int,
    currency: str,
    payload: SubscriptionPeriodPriceUpsertRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    normalized_currency = normalize_currency(currency)
    result = await db.execute(
        select(SubscriptionPeriodPrice).where(
            SubscriptionPeriodPrice.period_days == period_days,
            SubscriptionPeriodPrice.currency == normalized_currency,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.amount_minor = payload.amount_minor
        row.is_active = payload.is_active
    else:
        row = SubscriptionPeriodPrice(
            period_days=period_days,
            currency=normalized_currency,
            amount_minor=payload.amount_minor,
            is_active=payload.is_active,
        )
        db.add(row)

    await db.commit()
    await db.refresh(row)
    return SubscriptionPeriodPriceResponse(
        period_days=row.period_days,
        currency=row.currency,
        amount_minor=row.amount_minor,
        is_active=row.is_active,
    )


@router.delete('/subscription-period-prices/{period_days}/{currency}')
async def delete_subscription_period_price(
    period_days: int,
    currency: str,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    normalized_currency = normalize_currency(currency)
    result = await db.execute(
        select(SubscriptionPeriodPrice).where(
            SubscriptionPeriodPrice.period_days == period_days,
            SubscriptionPeriodPrice.currency == normalized_currency,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription period price not found')
    await db.delete(row)
    await db.commit()
    return {'success': True}


@router.get('/traffic-package-prices', response_model=list[TrafficPackagePriceResponse])
async def list_traffic_package_prices(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    result = await db.execute(
        select(TrafficPackagePrice).order_by(
            TrafficPackagePrice.package_gb,
            TrafficPackagePrice.currency,
        )
    )
    rows = list(result.scalars().all())
    return [
        TrafficPackagePriceResponse(
            package_gb=row.package_gb,
            currency=row.currency,
            amount_minor=row.amount_minor,
            is_active=row.is_active,
        )
        for row in rows
    ]


@router.put('/traffic-package-prices/{package_gb}/{currency}', response_model=TrafficPackagePriceResponse)
async def upsert_traffic_package_price(
    package_gb: int,
    currency: str,
    payload: TrafficPackagePriceUpsertRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    normalized_currency = normalize_currency(currency)
    result = await db.execute(
        select(TrafficPackagePrice).where(
            TrafficPackagePrice.package_gb == package_gb,
            TrafficPackagePrice.currency == normalized_currency,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.amount_minor = payload.amount_minor
        row.is_active = payload.is_active
    else:
        row = TrafficPackagePrice(
            package_gb=package_gb,
            currency=normalized_currency,
            amount_minor=payload.amount_minor,
            is_active=payload.is_active,
        )
        db.add(row)

    await db.commit()
    await db.refresh(row)
    return TrafficPackagePriceResponse(
        package_gb=row.package_gb,
        currency=row.currency,
        amount_minor=row.amount_minor,
        is_active=row.is_active,
    )


@router.delete('/traffic-package-prices/{package_gb}/{currency}')
async def delete_traffic_package_price(
    package_gb: int,
    currency: str,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    normalized_currency = normalize_currency(currency)
    result = await db.execute(
        select(TrafficPackagePrice).where(
            TrafficPackagePrice.package_gb == package_gb,
            TrafficPackagePrice.currency == normalized_currency,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Traffic package price not found')
    await db.delete(row)
    await db.commit()
    return {'success': True}
