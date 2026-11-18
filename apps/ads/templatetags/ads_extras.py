from decimal import Decimal

from django import template
from django.utils import formats

from apps.analytics.templatetags.analytics_extras import CURRENCY_SYMBOLS

register = template.Library()


@register.filter
def ads_money(value, currency: str = "") -> str:
    """An amount in whole units of the ad account's currency, e.g. ``R$ 1.234,50``."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return "—"
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper(), (currency or "").upper())
    number = formats.number_format(round(amount, 2), 2, use_l10n=True, force_grouping=True)
    return f"{symbol} {number}".strip()


@register.filter
def ads_percent(value) -> str:
    """Meta's ``ctr`` is already a percentage (1.5 means 1.5%)."""
    if value is None or value == "":
        return "—"
    return f"{formats.number_format(round(Decimal(str(value)), 2), 2, use_l10n=True)}%"
