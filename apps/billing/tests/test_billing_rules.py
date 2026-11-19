"""The billing rules on the plan screen: yearly discount, free trial, volume
discounts and the subscription lock. Stored like the prices, falling back to
the BILLING_* settings until the operator saves them."""

from __future__ import annotations

import pytest
from django.db.models.signals import post_save
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import services
from apps.billing.models import ProductPrice, ProductPriceChange

pytestmark = pytest.mark.django_db

RULES = reverse("billing_staff:update_rules")


@pytest.fixture(autouse=True)
def _clear_price_cache():
    services.invalidate_price_cache()
    yield
    services.invalidate_price_cache()


@pytest.fixture
def operator():
    post_save.disconnect(create_organization_on_user_create, sender=User)
    try:
        return User.objects.create_user(
            email="ops@example.com", password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
    finally:
        post_save.connect(create_organization_on_user_create, sender=User)


def _form(**overrides):
    data = {
        "annual_discount_percent": "20",
        "trial_days": "7",
        "volume_1_from": "5",
        "volume_1_percent": "10",
        "volume_2_from": "10",
        "volume_2_percent": "15",
        "volume_3_from": "",
        "volume_3_percent": "",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


@override_settings(
    BILLING_ANNUAL_DISCOUNT_PERCENT=5,
    BILLING_TRIAL_DAYS=3,
    BILLING_REQUIRE_SUBSCRIPTION=True,
    BILLING_VOLUME_DISCOUNTS=[(4, 8)],
)
def test_until_saved_the_rules_come_from_the_settings():
    assert services.annual_discount_percent() == 5
    assert services.trial_days() == 3
    assert services.subscription_required() is True
    assert services.volume_discounts() == [(4, 8)]
    assert services.volume_discount_percent(4) == 8
    assert services.billing_rules()["is_configured"] is False


@override_settings(BILLING_ANNUAL_DISCOUNT_PERCENT=0, BILLING_TRIAL_DAYS=0, BILLING_VOLUME_DISCOUNTS=[])
def test_saving_the_rules_makes_them_the_ones_in_force(client, operator):
    client.force_login(operator)

    response = client.post(RULES, _form(subscription_required="on"))

    assert response.status_code == 302
    assert services.annual_discount_percent() == 20
    assert services.trial_days() == 7
    assert services.subscription_required() is True
    assert services.volume_discounts() == [(5, 10), (10, 15)]
    assert [services.volume_discount_percent(n) for n in (4, 5, 9, 10, 30)] == [0, 10, 10, 15, 15]
    assert ProductPriceChange.objects.filter(key=ProductPrice.Key.TRIAL_DAYS, new_value=7).exists()


@override_settings(BILLING_VOLUME_DISCOUNTS=[(5, 10)], BILLING_REQUIRE_SUBSCRIPTION=True)
def test_emptied_tiers_and_an_unticked_lock_turn_them_off(client, operator):
    client.force_login(operator)

    client.post(RULES, _form(volume_1_from="", volume_1_percent="", volume_2_from="", volume_2_percent=""))

    assert services.volume_discounts() == []
    assert services.subscription_required() is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"annual_discount_percent": "91"},
        {"trial_days": "-1"},
        {"trial_days": "abc"},
        {"volume_1_from": "5", "volume_1_percent": ""},
        {"volume_1_from": "1", "volume_1_percent": "10"},
        {"volume_2_from": "5", "volume_2_percent": "20"},
    ],
)
def test_an_invalid_form_writes_nothing(client, operator, overrides):
    client.force_login(operator)

    client.post(RULES, _form(**overrides))

    assert not ProductPrice.objects.exists()


def test_only_staff_save_the_rules(client):
    client.force_login(
        User.objects.create_user(email="member@example.com", password="x", tos_accepted_at=timezone.now())
    )

    assert client.post(RULES, _form()).status_code == 403
    assert not ProductPrice.objects.exists()


def test_the_plan_screen_shows_the_rules(client, operator):
    client.force_login(operator)
    client.post(RULES, _form())

    response = client.get(reverse("billing_staff:plans"))

    rules = response.context["rules"]
    assert rules["annual_discount_percent"] == 20 and rules["trial_days"] == 7
    assert [(tier["from"], tier["percent"]) for tier in rules["volume"]] == [(5, 10), (10, 15), ("", "")]
    assert reverse("billing_staff:update_rules") in response.content.decode()
