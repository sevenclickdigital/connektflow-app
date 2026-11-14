"""The month the Publish page shows: the side panel and the list tabs."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _account(workspace, platform):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{workspace.id}",
        account_name=platform,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _post(account, when, status="scheduled", caption="post"):
    post = Post.objects.create(workspace=account.workspace, caption=caption)
    return PlatformPost.objects.create(post=post, social_account=account, scheduled_at=when, status=status)


@pytest.fixture
def workspace(organization):
    return Workspace.objects.create(organization=organization, name="Acme Social")


@pytest.fixture
def client_in(workspace, org_owner):
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client = Client()
    client.force_login(org_owner)
    return client


def _month(client, workspace, **params):
    url = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    return client.get(url, {"mode": "calendar", "view": "month"} | params)


def test_summary_counts_this_workspaces_posts_per_platform(workspace, client_in, organization):
    soon = timezone.now() + timedelta(hours=1)
    instagram = _account(workspace, "instagram")
    facebook = _account(workspace, "facebook")
    _post(instagram, soon)
    _post(instagram, soon)
    _post(facebook, soon)
    other = Workspace.objects.create(organization=organization, name="Other")
    _post(_account(other, "instagram"), soon)

    summary = _month(client_in, workspace, date=soon.date().isoformat()).context["month_summary"]

    assert summary["total"] == 3
    assert [(p["platform"], p["count"]) for p in summary["platforms"]] == [("instagram", 2), ("facebook", 1)]


def test_upcoming_posts_are_future_still_due_and_of_this_workspace_only(workspace, client_in, organization):
    now = timezone.now()
    account = _account(workspace, "instagram")
    due = _post(account, now + timedelta(hours=2), caption="due")
    _post(account, now + timedelta(hours=1), status="published", caption="already out")
    _post(account, now + timedelta(hours=1), status="failed", caption="failed")
    _post(account, now - timedelta(hours=1), caption="past")
    other = Workspace.objects.create(organization=organization, name="Other")
    _post(_account(other, "instagram"), now + timedelta(minutes=5), caption="other workspace")

    upcoming = _month(client_in, workspace).context["upcoming_posts"]

    assert [item["pp"].id for item in upcoming] == [due.id]


def test_upcoming_posts_follow_the_platform_filter(workspace, client_in):
    soon = timezone.now() + timedelta(hours=1)
    _post(_account(workspace, "instagram"), soon)
    facebook_post = _post(_account(workspace, "facebook"), soon + timedelta(minutes=5))

    upcoming = _month(client_in, workspace, platform="facebook").context["upcoming_posts"]

    assert [item["pp"].id for item in upcoming] == [facebook_post.id]


@pytest.mark.parametrize("view", ["week", "day"])
def test_week_and_day_views_have_the_side_panel_too(workspace, client_in, view):
    soon = timezone.now() + timedelta(hours=1)
    due = _post(_account(workspace, "instagram"), soon)

    context = _month(client_in, workspace, view=view, date=soon.date().isoformat()).context

    assert context["month_summary"]["total"] == 1
    assert [item["pp"].id for item in context["upcoming_posts"]] == [due.id]


def test_a_week_inside_one_month_names_the_month_once():
    from datetime import date

    from django.utils import translation

    from apps.calendar.services import week_label

    # Month names come from Django's own catalogue; the range's wording from
    # ours, which CI runs without compiling, so only the parts are checked.
    with translation.override("pt-br"):
        label = week_label(date(2026, 11, 9), date(2026, 11, 15))
        assert label.count("Novembro") == 1
        assert "9–15" in label
        assert "2026" in label
        assert "Dezembro" in week_label(date(2026, 11, 30), date(2026, 12, 6))


def _tab(client, workspace, tab, **params):
    url = reverse(f"calendar:publish_tab_{tab}", kwargs={"workspace_id": workspace.id})
    return client.get(url, params, HTTP_HX_REQUEST="true")


def test_list_tabs_show_only_the_month_chosen_and_count_the_same(workspace, client_in):
    from datetime import UTC, datetime

    account = _account(workspace, "instagram")
    in_november = _post(account, datetime(2026, 11, 20, 12, tzinfo=UTC))
    _post(account, datetime(2026, 12, 2, 12, tzinfo=UTC))

    response = _tab(client_in, workspace, "queue", date="2026-11-01")

    assert [pp.id for pp in response.context["platform_posts"]] == [in_november.id]
    assert response.context["queue_count"] == 1
    assert response.context["month_summary"]["total"] == 1
    assert b'id="cal-side-panel" class="cal-side" hx-swap-oob="true"' in response.content


def test_an_undated_draft_is_listed_in_every_month(workspace, client_in):
    undated = _post(_account(workspace, "instagram"), None, status="draft")

    for month in ("2026-01-01", "2026-11-01"):
        response = _tab(client_in, workspace, "drafts", date=month)
        assert [pp.id for pp in response.context["platform_posts"]] == [undated.id]


def _board(client, workspace, **params):
    url = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    return client.get(url, {"mode": "board"} | params, HTTP_HX_REQUEST="true")


def _board_ids(response):
    return {pp.id for entry in response.context["status_board"] for pp in entry["cards"]}


def test_board_shows_the_month_chosen_plus_undated_cards(workspace, client_in):
    from datetime import UTC, datetime

    account = _account(workspace, "instagram")
    in_november = _post(account, datetime(2026, 11, 20, 12, tzinfo=UTC))
    _post(account, datetime(2026, 12, 2, 12, tzinfo=UTC))
    undated = _post(account, None, status="draft")

    response = _board(client_in, workspace, date="2026-11-01", _columns="1")

    assert _board_ids(response) == {in_november.id, undated.id}
    assert [t.name for t in response.templates][0] == "calendar/partials/_board_columns.html"


def test_board_follows_the_platform_filter(workspace, client_in):
    soon = timezone.now() + timedelta(hours=1)
    _post(_account(workspace, "instagram"), soon)
    facebook_post = _post(_account(workspace, "facebook"), soon)

    response = _board(client_in, workspace, platform="facebook", _columns="1")

    assert _board_ids(response) == {facebook_post.id}


def _day(client, workspace, **params):
    url = reverse("calendar:day_posts", kwargs={"workspace_id": workspace.id})
    return client.get(url, params, HTTP_HX_REQUEST="true")


def test_day_pop_up_lists_only_that_day_of_this_workspace(workspace, client_in, organization):
    from datetime import UTC, datetime

    account = _account(workspace, "instagram")
    on_the_day = _post(account, datetime(2026, 11, 20, 12, tzinfo=UTC))
    _post(account, datetime(2026, 11, 21, 12, tzinfo=UTC))
    other = Workspace.objects.create(organization=organization, name="Other")
    _post(_account(other, "instagram"), datetime(2026, 11, 20, 13, tzinfo=UTC))

    response = _day(client_in, workspace, date="2026-11-20")

    assert response.status_code == 200
    assert [pp.id for pp in response.context["platform_posts"]] == [on_the_day.id]


def test_day_pop_up_refuses_a_workspace_the_user_is_not_in(client_in, organization):
    other = Workspace.objects.create(organization=organization, name="Other")

    assert _day(client_in, other, date="2026-11-20").status_code == 403


@pytest.mark.parametrize(
    ("platform", "fmt", "ratio"),
    [
        ("instagram", "story", "9:16"),
        ("instagram", "reel", "9:16"),
        ("instagram", "default", "4:5 – 1.91:1"),
        ("facebook", "default", "1:1 / 4:5"),
        ("youtube", "default", "16:9"),
    ],
)
def test_the_post_card_shows_the_format_and_its_proportion(workspace, client_in, platform, fmt, ratio):
    pp = _post(_account(workspace, platform), timezone.now() + timedelta(days=1))
    pp.format = fmt
    pp.save(update_fields=["format"])

    url = reverse("calendar:post_card", kwargs={"workspace_id": workspace.id, "platform_post_id": pp.id})
    body = client_in.get(url).content.decode()

    assert f"{pp.get_format_display()} ({ratio})" in body
