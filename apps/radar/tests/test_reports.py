"""Shopee Affiliates analytics: what the report counts, and who can open it."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.radar import reports
from apps.radar.models import OpportunityConnection
from apps.workspaces.models import Workspace
from providers.exceptions import RateLimitError
from providers.shopee_affiliate import Conversion, ConversionReport

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english", "_clear_cache")]

SAO_PAULO = zoneinfo.ZoneInfo("America/Sao_Paulo")
CONVERSIONS = "providers.shopee_affiliate.ShopeeAffiliateClient.conversions"


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _workspace(name):
    org = Organization.objects.create(name=f"{name} org")
    return Workspace.objects.create(organization=org, name=name, timezone="America/Sao_Paulo")


def _member(email, workspace):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    return user


def _connection(workspace):
    return OpportunityConnection.objects.create(
        workspace=workspace,
        source=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        account_label="18340070022",
        credentials={"app_id": "18340070022", "secret": "s3cret"},
    )


def _conversion(order_id, purchased_at, *, status="COMPLETED", commission="2", amount="20", referrer="WhatsApp"):
    return Conversion(
        order_id=order_id,
        purchased_at=purchased_at,
        status=status,
        commission=Decimal(commission),
        amount=Decimal(amount),
        quantity=1,
        referrer=referrer,
        buyer_type="NEW",
        item_id=1,
        item_name="Guarda-chuva",
        item_count=1,
        image_url="https://cf.shopee.com.br/file/u",
        shop_name="Loja",
    )


def _today_at(hour, days_ago=0):
    local = timezone.now().astimezone(SAO_PAULO) - timedelta(days=days_ago)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def workspace():
    return _workspace("Acme")


@pytest.fixture
def connection(workspace):
    return _connection(workspace)


def _card(report, metric):
    return next(c["derived"] for c in report["hero_cards"] if c["metric"] == metric)


def test_cancelled_orders_count_only_as_cancelled(connection):
    conversions = [
        _conversion("A", _today_at(10), commission="3", amount="30"),
        _conversion("B", _today_at(11), status="PENDING", commission="1.5", amount="15"),
        _conversion("C", _today_at(12), status="CANCELLED", commission="0", amount="99"),
    ]
    with patch(CONVERSIONS, return_value=ConversionReport(conversions)):
        report = reports.build_report(connection, days=7, tz=SAO_PAULO)

    assert _card(report, "commission").value == 4.5
    assert _card(report, "orders").value == 2
    assert _card(report, "sales").value == 45
    assert report["average_commission"] == Decimal("2.25")
    assert {s["key"]: s["orders"] for s in report["status_summary"]} == {"COMPLETED": 1, "PENDING": 1, "CANCELLED": 1}
    assert report["table"]["total"] == 3


def test_days_are_the_workspace_days(connection):
    # 01:00 UTC is still the previous evening in São Paulo.
    late_evening = _today_at(22, days_ago=8)
    with patch(CONVERSIONS, return_value=ConversionReport([_conversion("A", late_evening.astimezone(UTC))])):
        report = reports.build_report(connection, days=7, tz=SAO_PAULO)

    # Eight days ago is in the previous 7-day period, not the current one.
    assert _card(report, "orders").value == 0
    assert report["table"]["total"] == 0


def test_only_ranges_shopee_fully_reports_have_a_comparison(connection):
    with patch(CONVERSIONS, return_value=ConversionReport([_conversion("A", _today_at(9))])):
        thirty = reports.build_report(connection, days=30, tz=SAO_PAULO)
        ninety = reports.build_report(connection, days=90, tz=SAO_PAULO)

    assert thirty["comparable"] and _card(thirty, "commission").comparison_available
    assert not ninety["comparable"] and not _card(ninety, "commission").comparison_available


def test_report_is_read_from_shopee_once_per_cache_window(connection):
    with patch(CONVERSIONS, return_value=ConversionReport([])) as fetch:
        reports.build_report(connection, days=7, tz=SAO_PAULO)
        reports.build_report(connection, days=30, tz=SAO_PAULO)

    assert fetch.call_count == 1


def _page(workspace, connection_id):
    return reverse("analytics:shopee", kwargs={"workspace_id": workspace.id, "connection_id": connection_id})


def test_member_sees_the_commission(workspace, connection):
    client = Client()
    client.force_login(_member("viewer@example.com", workspace))
    with patch(
        CONVERSIONS, return_value=ConversionReport([_conversion("A", _today_at(9), commission="12.5")])
    ) as fetch:
        page = client.get(_page(workspace, connection.id))
        assert not fetch.called  # the page shows first; the orders load after
        response = client.get(_page(workspace, connection.id), {"partial": "body"}, HTTP_HX_REQUEST="true")

    assert page.status_code == 200
    assert response.status_code == 200
    assert "R$\xa012.50" in response.content.decode()
    assert "s3cret" not in response.content.decode()


def test_another_workspace_connection_is_not_found(workspace):
    other = _workspace("Other")
    foreign = _connection(other)
    client = Client()
    client.force_login(_member("viewer@example.com", workspace))

    with patch(CONVERSIONS) as fetch:
        response = client.get(_page(workspace, foreign.id))

    assert response.status_code == 404
    fetch.assert_not_called()


def test_non_member_is_refused(workspace, connection):
    outsider = _member("outsider@example.com", _workspace("Other"))
    client = Client()
    client.force_login(outsider)

    with patch(CONVERSIONS) as fetch:
        response = client.get(_page(workspace, connection.id))

    assert response.status_code == 403
    fetch.assert_not_called()


def test_shopee_failure_is_shown(workspace, connection):
    client = Client()
    client.force_login(_member("viewer@example.com", workspace))
    with patch(CONVERSIONS, side_effect=RateLimitError("Shopee Affiliate error 10030", platform="Shopee Affiliate")):
        response = client.get(_page(workspace, connection.id), {"partial": "body"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert "Shopee is limiting requests right now" in response.content.decode()


def test_analytics_opens_on_shopee_when_there_is_no_channel(workspace, connection):
    client = Client()
    client.force_login(_member("viewer@example.com", workspace))

    response = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}))

    assert response.status_code == 302
    assert response.url == _page(workspace, connection.id)


def test_date_is_shown_in_the_workspace_zone(workspace, connection):
    purchased = datetime(2026, 11, 10, 1, 30, tzinfo=UTC)
    table = reports._table([_conversion("A", purchased)], status="all", page=1, tz=SAO_PAULO)

    assert table["rows"][0]["date"].startswith("11/09/2026")


def test_a_custom_period_counts_only_the_orders_of_its_days(connection):
    end = _today_at(12, days_ago=10).date()
    conversions = [
        _conversion("in", _today_at(10, days_ago=12)),
        _conversion("after", _today_at(10, days_ago=3)),
        _conversion("before", _today_at(10, days_ago=20)),
    ]
    with patch(CONVERSIONS, return_value=ConversionReport(conversions)):
        report = reports.build_report(connection, days=5, end=end, tz=SAO_PAULO)

    assert _card(report, "orders").value == 1
    assert report["table"]["total"] == 1
    assert report["chart"]["labels"][-1] == end.isoformat()
