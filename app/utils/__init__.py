__all__ = [
    'calculate_months_from_days',
    'calculate_prorated_price',
    'format_period_description',
    'get_remaining_months',
]


def calculate_months_from_days(*args, **kwargs):
    from .pricing_utils import calculate_months_from_days as _impl

    return _impl(*args, **kwargs)


def calculate_prorated_price(*args, **kwargs):
    from .pricing_utils import calculate_prorated_price as _impl

    return _impl(*args, **kwargs)


def format_period_description(*args, **kwargs):
    from .pricing_utils import format_period_description as _impl

    return _impl(*args, **kwargs)


def get_remaining_months(*args, **kwargs):
    from .pricing_utils import get_remaining_months as _impl

    return _impl(*args, **kwargs)
