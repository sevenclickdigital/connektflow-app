"""HTTP-level tests for the analytics page's account list.

Regression cover for accounts disappearing from the account switcher. A
platform switched off in ``AnalyticsPlatformConfig`` used to be filtered out of
the queryset feeding the switcher, so a connected Instagram (Direct) account
simply wasn't there and nothing anywhere said why.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics import services as analytics_services
from apps.analytics.models import AccountInsightsSnapshot
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Analytics Views Org")
    return Workspace.objects.create(organization=org, name="Analytics Views WS")


@pytest.fixture
def owner_client(client, workspace):
    user = User.objects.create_user(
        email="analytics-views@example.com",
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    OrgMembership.objects.create(
        user=user,
        organization=workspace.organization,
        org_role=OrgMembership.OrgRole.OWNER,
    )
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )
    client.force_login(user)
    return client


def _account(workspace, platform, name):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=name,
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _disable_analytics(platform):
    AnalyticsPlatformConfig.objects.update_or_create(platform=platform, defaults={"is_enabled": False})


def _account_url(workspace, account):
    return reverse("analytics:account", kwargs={"workspace_id": workspace.id, "account_id": account.id})


@pytest.mark.django_db
def test_instagram_direct_account_is_listed_and_available(owner_client, workspace):
    account = _account(workspace, "instagram_login", "Direct IG")

    response = owner_client.get(_account_url(workspace, account))

    assert response.status_code == 200
    assert [a.id for a in response.context["accounts"]] == [account.id]
    assert response.context["analytics_unavailable"] is False


@pytest.mark.django_db
def test_disabled_platform_account_stays_listed_with_a_reason(owner_client, workspace, settings):
    """The reported bug: the account vanished from the switcher entirely.

    It must still be listed, and selecting it must carry the admin note saying
    where the platform is switched on. Anchored on the note's id rather than its
    prose so the copy can be reworded without touching this test.
    """
    # The marker below is translated; the project's default language is pt-br.
    settings.LANGUAGE_CODE = "en-us"
    account = _account(workspace, "instagram_login", "Direct IG")
    _disable_analytics("instagram_login")

    response = owner_client.get(_account_url(workspace, account))

    assert response.status_code == 200
    assert [a.id for a in response.context["accounts"]] == [account.id]
    assert response.context["analytics_unavailable"] is True
    assert response.context["analytics_disabled_by_admin"] is True
    assert response.context["accounts"][0].analytics_unavailable_reason
    body = response.content.decode()
    assert 'id="analytics-disabled-admin-note"' in body
    # The switcher marks it rather than listing it indistinguishably.
    assert "No data" in body


@pytest.mark.django_db
def test_index_lands_on_the_only_account_even_when_disabled(owner_client, workspace):
    """Better to land on the explanation than on "connect an account"."""
    account = _account(workspace, "instagram_login", "Direct IG")
    _disable_analytics("instagram_login")

    response = owner_client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}), follow=True)

    assert response.status_code == 200
    assert response.context["active_account"].id == account.id
    assert response.context["analytics_unavailable"] is True


@pytest.mark.django_db
def test_index_prefers_an_account_with_analytics_available(owner_client, workspace):
    disabled = _account(workspace, "facebook", "AAA Disabled")
    available = _account(workspace, "instagram_login", "ZZZ Available")
    _disable_analytics("facebook")

    response = owner_client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id}), follow=True)

    # "facebook" sorts first and would have been the redirect target on
    # ordering alone; availability wins.
    assert disabled.platform < available.platform
    assert response.context["active_account"].id == available.id


@pytest.mark.django_db
def test_inherently_unavailable_platform_gets_no_reconnect_hint(owner_client, workspace):
    """Bluesky has no analytics API — reconnecting would achieve nothing."""
    account = _account(workspace, "bluesky", "Bluesky")

    response = owner_client.get(_account_url(workspace, account))

    assert response.context["analytics_unavailable"] is True
    assert response.context["analytics_disabled_by_admin"] is False
    assert "Reconnect this account" not in response.content.decode()


@pytest.mark.django_db
def test_disconnected_account_is_not_listed(owner_client, workspace):
    """The connection-status filter is the one that still drops accounts."""
    listed = _account(workspace, "instagram_login", "Direct IG")
    dropped = _account(workspace, "threads", "Threads")
    dropped.connection_status = SocialAccount.ConnectionStatus.ERROR
    dropped.save(update_fields=["connection_status"])

    response = owner_client.get(_account_url(workspace, listed))

    assert [a.id for a in response.context["accounts"]] == [listed.id]


@pytest.mark.django_db
def test_devto_account_renders_the_unavailable_state(owner_client, workspace):
    """DEV.to has no metrics methods and no entry in the PLATFORM_* maps.

    It reaches the page now that a missing config row counts as enabled, so the
    platform-keyed lookups all have to tolerate it rather than KeyError.
    """
    account = _account(workspace, "devto", "Dev Blog")

    response = owner_client.get(_account_url(workspace, account))

    assert response.status_code == 200
    assert response.context["analytics_unavailable"] is True
    assert response.context["analytics_disabled_by_admin"] is False


@pytest.mark.django_db
def test_unavailable_account_suppresses_the_scope_reconnect_banner(owner_client, workspace):
    """Two reconnect CTAs on one screen give contradictory instructions.

    The unavailable state carries its own, so the insufficient-scope banner
    stands down while it's showing.
    """
    account = _account(workspace, "instagram_login", "Direct IG")
    account.analytics_needs_reconnect = True
    account.save(update_fields=["analytics_needs_reconnect"])
    _disable_analytics("instagram_login")

    response = owner_client.get(_account_url(workspace, account))

    assert response.context["analytics_needs_reconnect"] is False
    assert "insufficient-scope" not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("platform", ["instagram_login", "bluesky"])
def test_pages_never_leak_raw_template_tags(owner_client, workspace, platform):
    """Django's ``{# #}`` is single-line only: a multi-line one is emitted as
    visible page text instead of being stripped. Cheap to assert, and it caught
    two real leaks in the account switcher and the unavailable state.
    """
    account = _account(workspace, platform, "Acct")
    _disable_analytics(platform)

    body = owner_client.get(_account_url(workspace, account)).content.decode()

    assert "{#" not in body
    assert "{%" not in body


@pytest.mark.django_db
def test_youtube_dashboard_ignores_legacy_account_estimates(owner_client, workspace):
    """Legacy rows cannot supply native YouTube cards or engagement rates."""
    account = _account(workspace, "youtube", "Tube")
    today = timezone.now().date()
    for offset in range(14):
        for metric, value in {
            "views": 100,
            "likes": 8,
            "comments": 2,
            "shares": 1,
            "watch_time": 30,
            "avg_view_pct": 45,
        }.items():
            AccountInsightsSnapshot.objects.create(
                social_account=account,
                metric_key=metric,
                date=today - timedelta(days=offset),
                value=value,
            )

    response = owner_client.get(_account_url(workspace, account) + "?range=7")
    body = response.content.decode()

    assert response.context["engagement"] is None
    assert "Engagement rate" not in body
    assert "Daily average · calculated by Connekt Flow" not in body
    assert "Metrics reported by YouTube" in body
    assert all(not card["derived"].available for card in response.context["hero_cards"])


@pytest.mark.django_db
def test_youtube_daily_counts_are_not_reconstructed_from_lifetime_snapshots(owner_client, workspace):
    """Missing native daily reports stay unavailable despite lifetime snapshots."""
    from apps.analytics.models import PostInsightsSnapshot
    from apps.composer.models import PlatformPost, Post

    account = _account(workspace, "youtube", "Tube")
    platform_post = PlatformPost.objects.create(
        post=Post.objects.create(workspace=workspace, caption="video"),
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
        published_at=timezone.now() - timedelta(days=60),
        platform_post_id="video-1",
    )
    today = timezone.now().date()
    for offset, views in ((3, 100), (1, 160)):
        PostInsightsSnapshot.objects.create(
            platform_post=platform_post, metric_key="views", date=today - timedelta(days=offset), value=views
        )

    response = owner_client.get(_account_url(workspace, account) + "?range=7")
    body = response.content.decode()

    assert "Estimated by Connekt Flow from per-video counts" not in body
    assert response.context["chart"]["derived"].estimated is False
    assert response.context["chart"]["derived"].available is False
    # Today has no snapshot yet: a gap in the chart, not a drop to zero views.
    assert response.context["chart_series_json"].endswith("null]")


def _member_client(workspace, role):
    user = User.objects.create_user(
        email=f"analytics-{role}@example.com", password="testpass123", tos_accepted_at=timezone.now()
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    member_client = Client()
    member_client.force_login(user)
    return member_client


def _read_urls(workspace, account):
    ids = {"workspace_id": workspace.id}
    pp = PlatformPost.objects.create(
        post=Post.objects.create(workspace=workspace, caption="measured"),
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
    )
    return [
        reverse("analytics:index", kwargs=ids),
        reverse("analytics:account", kwargs={**ids, "account_id": account.id}),
        reverse("analytics:status", kwargs={**ids, "account_id": account.id}),
        reverse("analytics:history_offer", kwargs={**ids, "account_id": account.id}),
        reverse("analytics:post_detail", kwargs={**ids, "post_id": pp.id}),
        reverse("analytics:shopee", kwargs={**ids, "connection_id": uuid.uuid4()}),
    ]


@pytest.mark.django_db
def test_member_without_view_analytics_is_refused_on_every_page(workspace):
    account = _account(workspace, "instagram_login", "Direct IG")
    contributor = _member_client(workspace, WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)

    with patch("apps.analytics.history_import.importable_count") as count:
        statuses = [contributor.get(url).status_code for url in _read_urls(workspace, account)]

    assert statuses == [403] * 6
    count.assert_not_called()


@pytest.mark.django_db
def test_viewer_still_reads_analytics(workspace):
    account = _account(workspace, "instagram_login", "Direct IG")
    viewer = _member_client(workspace, WorkspaceMembership.WorkspaceRole.VIEWER)

    response = viewer.get(_account_url(workspace, account))

    assert response.status_code == 200


@pytest.mark.django_db
def test_sidebar_reports_link_follows_view_analytics(workspace):
    _account(workspace, "instagram_login", "Direct IG")
    link = reverse("analytics:index", kwargs={"workspace_id": workspace.id})
    page = reverse("calendar:queue_list", kwargs={"workspace_id": workspace.id})

    contributor = _member_client(workspace, WorkspaceMembership.WorkspaceRole.CONTRIBUTOR).get(page)
    viewer = _member_client(workspace, WorkspaceMembership.WorkspaceRole.VIEWER).get(page)

    assert link not in contributor.content.decode()
    assert link in viewer.content.decode()


@pytest.mark.django_db
def test_the_posts_table_loads_after_the_page(owner_client, workspace):
    account = _account(workspace, "instagram_login", "Direct IG")
    AccountInsightsSnapshot.objects.create(
        social_account=account, metric_key="impressions", date=timezone.now().date(), value=10
    )
    url = _account_url(workspace, account)

    with patch(
        "apps.analytics.services.all_posts_for",
        wraps=analytics_services.all_posts_for,
    ) as posts:
        page = owner_client.get(url, {"range": "7", "type": "reels"})
        assert not posts.called
        table = owner_client.get(url, {"range": "7", "type": "reels", "partial": "table"}, HTTP_HX_REQUEST="true")

    assert page.context["table"] is None
    assert 'id="analytics-post-table" hx-get=' in page.content.decode()
    assert "type=reels&amp;partial=table" in page.content.decode()
    assert table.status_code == 200
    assert table.context["table"] is not None
