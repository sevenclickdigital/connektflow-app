"""Radar Instagram research: watched profiles (Business Discovery) and hashtags."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.radar import instagram as research
from apps.radar.models import WatchedProfile
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError
from providers.instagram import InstagramProvider

from .test_radar import Role, _client, _user, _workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english")]


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


def _instagram(workspace, ig_id="ig-1"):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id=ig_id,
        account_name="Brand",
        oauth_access_token="tok",
    )


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _meta_error(code):
    return APIError("refused", status_code=400, platform="instagram", raw_response={"error": {"code": code}})


class TestWatchedProfiles:
    def test_a_handle_is_saved_once_and_opened(self):
        workspace = _workspace("A")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))
        url = reverse("radar:watch_profile", kwargs={"workspace_id": workspace.id})

        response = client.post(url, {"username": "@Rival.Shop"})
        client.post(url, {"username": "rival.shop"})

        assert WatchedProfile.objects.filter(workspace=workspace).count() == 1
        assert WatchedProfile.objects.get(workspace=workspace).username == "rival.shop"
        assert "u=rival.shop" in response["Location"]

    def test_an_invalid_handle_is_refused(self):
        with pytest.raises(research.InvalidInputError):
            research.clean_handle("not a handle!")

    def test_another_workspace_cannot_remove_a_watched_profile(self):
        owner, other = _workspace("A"), _workspace("B")
        profile = WatchedProfile.objects.create(workspace=owner, username="rival")
        client = _client(_user("b@x.com", other, Role.EDITOR))

        response = client.post(
            reverse("radar:unwatch_profile", kwargs={"workspace_id": other.id, "profile_id": profile.id})
        )

        assert response.status_code == 404
        assert WatchedProfile.objects.filter(pk=profile.pk).exists()

    def test_a_viewer_cannot_watch_profiles(self):
        workspace = _workspace("A")
        client = _client(_user("v@x.com", workspace, Role.VIEWER))

        response = client.post(
            reverse("radar:watch_profile", kwargs={"workspace_id": workspace.id}), {"username": "rival"}
        )

        assert response.status_code == 403
        assert not WatchedProfile.objects.exists()


class TestResearchScreen:
    def test_the_profile_report_shows_its_numbers(self):
        workspace = _workspace("A")
        _instagram(workspace)
        client = _client(_user("a@x.com", workspace, Role.EDITOR))
        discovery = {
            "username": "rival",
            "followers_count": 1000,
            "media_count": 50,
            "media": [
                {"id": "1", "like_count": 40, "comments_count": 10, "permalink": "https://www.instagram.com/p/1/"},
                {"id": "2", "like_count": 20, "comments_count": 10, "permalink": "https://www.instagram.com/p/2/"},
                # Likes hidden: left out of the mean.
                {"id": "3", "comments_count": 99, "permalink": "https://www.instagram.com/p/3/"},
            ],
        }

        with patch.object(InstagramProvider, "business_discovery", return_value=discovery) as call:
            response = client.get(reverse("radar:instagram", kwargs={"workspace_id": workspace.id}), {"u": "rival"})

        assert call.call_args.args[1] == "rival"
        report = response.context["report"]
        assert (report.avg_interactions, report.engagement_rate) == (40, 4.0)
        assert "https://www.instagram.com/p/3/" in response.content.decode()

    def test_another_workspaces_account_is_never_read_through(self):
        mine, theirs = _workspace("A"), _workspace("B")
        _instagram(mine, "ig-mine")
        foreign = _instagram(theirs, "ig-theirs")
        client = _client(_user("a@x.com", mine, Role.EDITOR))

        with patch.object(InstagramProvider, "business_discovery", return_value={"media": []}):
            response = client.get(
                reverse("radar:instagram", kwargs={"workspace_id": mine.id}), {"u": "rival", "account": foreign.id}
            )

        assert response.context["account"].account_platform_id == "ig-mine"

    def test_a_hashtag_search_meta_refuses_says_what_to_enable(self):
        workspace = _workspace("A")
        _instagram(workspace)
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        with patch.object(InstagramProvider, "hashtag_media", side_effect=_meta_error(10)):
            response = client.get(
                reverse("radar:instagram", kwargs={"workspace_id": workspace.id}), {"tab": "hashtags", "tag": "#cafe"}
            )

        assert "Instagram Public Content Access" in response.context["error"]

    def test_an_unknown_profile_says_only_professional_accounts_are_read(self):
        assert "business or creator" in research.describe_error(_meta_error(110), kind="profile")


class TestInstagramProviderResearch:
    def test_business_discovery_asks_for_the_profile_and_its_posts(self):
        provider = InstagramProvider({"ig_user_id": "ig-1"})
        provider._request = MagicMock(
            return_value=_resp({"business_discovery": {"username": "rival", "media": {"data": [{"id": "1"}]}}})
        )

        profile = provider.business_discovery("tok", "@rival")

        assert provider._request.call_args.args[1].endswith("/ig-1")
        assert provider._request.call_args.kwargs["params"]["fields"].startswith("business_discovery.username(rival){")
        assert profile["media"] == [{"id": "1"}]

    def test_hashtag_media_looks_up_the_id_then_reads_the_edge(self):
        provider = InstagramProvider({"ig_user_id": "ig-1"})
        provider._request = MagicMock(side_effect=[_resp({"data": [{"id": "h-1"}]}), _resp({"data": [{"id": "m-1"}]})])

        posts = provider.hashtag_media("tok", "#cafe", edge="recent_media")

        search, edge = provider._request.call_args_list
        assert search.kwargs["params"] == {"user_id": "ig-1", "q": "cafe"}
        assert edge.args[1].endswith("/h-1/recent_media")
        assert edge.kwargs["params"]["user_id"] == "ig-1"
        assert posts == [{"id": "m-1"}]
