from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from . import services


class ShopeeConnectForm(forms.Form):
    app_id = forms.RegexField(
        label=_("App ID"),
        regex=r"^\d{4,20}$",
        error_messages={"invalid": _("The App ID is the number shown in the Shopee affiliate Open API panel.")},
    )
    secret = forms.CharField(label=_("Secret"), max_length=128, strip=True, widget=forms.PasswordInput)


class OfferFilterForm(forms.Form):
    """Query-string filters of the Shopee offers screen. Invalid values are ignored, never an error page."""

    q = forms.CharField(required=False, max_length=100, strip=True)
    category = forms.TypedChoiceField(
        required=False,
        coerce=int,
        empty_value=None,
        choices=[("", "")] + [(str(cid), label) for cid, label in services.SHOPEE_CATEGORIES],
    )
    sort = forms.ChoiceField(required=False, choices=services.SORT_CHOICES)
    quick = forms.ChoiceField(required=False, choices=[("", "")] + [(q.key, q.label) for q in services.QUICK_FILTERS])
    min_price = forms.DecimalField(required=False, min_value=Decimal(0), max_digits=10, decimal_places=2)
    max_price = forms.DecimalField(required=False, min_value=Decimal(0), max_digits=10, decimal_places=2)
    min_discount = forms.IntegerField(required=False, min_value=0, max_value=100)
    min_rating = forms.DecimalField(required=False, min_value=Decimal(0), max_value=Decimal(5), decimal_places=1)
    min_sales = forms.IntegerField(required=False, min_value=0)
    min_commission = forms.DecimalField(required=False, min_value=Decimal(0), max_value=Decimal(100), decimal_places=1)
    pages = forms.IntegerField(required=False, min_value=1, max_value=services.MAX_PAGES)

    def to_filters(self) -> services.OfferFilters:
        """Build filters from whatever was valid; a bad field falls back to its default."""
        self.is_valid()
        data = {name: self.cleaned_data.get(name) for name in self.fields if name not in self.errors}
        return services.OfferFilters(
            keyword=data.get("q") or "",
            category_id=data.get("category") or None,
            sort=data.get("sort") or services.DEFAULT_SORT,
            quick=data.get("quick") or "",
            min_price=data.get("min_price"),
            max_price=data.get("max_price"),
            min_discount=data.get("min_discount"),
            min_rating=data.get("min_rating"),
            min_sales=data.get("min_sales"),
            min_commission_pct=data.get("min_commission"),
            pages=data.get("pages") or 1,
        )
