"""The page banners' "See how it works" videos: reading YouTube links, the
operator's screen that sets them, and what each banner shows."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.onboarding import help_videos
from apps.onboarding.models import PageHelpVideo

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?feature=share&v={VIDEO_ID}&t=10s",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=abc",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"  https://www.youtube-nocookie.com/embed/{VIDEO_ID}  ",
    ],
)
def test_every_shape_of_youtube_link_gives_the_video_id(url):
    assert help_videos.youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a link",
        f"https://vimeo.com/{VIDEO_ID}",
        f"https://evil.example.com/watch?v={VIDEO_ID}",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=short",
        f"javascript:alert(1)//youtu.be/{VIDEO_ID}",
    ],
)
def test_anything_but_a_youtube_video_is_refused(url):
    assert help_videos.youtube_video_id(url) is None


def _user(email, *, staff=False):
    return User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now(), is_staff=staff)


@pytest.mark.django_db
def test_the_operator_sets_replaces_and_removes_a_page_video(client):
    client.force_login(_user("op@example.com", staff=True))
    save = reverse("onboarding_staff:help_video_save")

    assert client.get(reverse("onboarding_staff:help_videos")).status_code == 200
    client.post(save, {"page": "dashboard", "video_url": f"https://youtu.be/{VIDEO_ID}"})
    assert PageHelpVideo.objects.get(page="dashboard").video_url == f"https://youtu.be/{VIDEO_ID}"

    client.post(save, {"page": "dashboard", "video_url": "https://vimeo.com/123"})
    assert PageHelpVideo.objects.get(page="dashboard").video_url == f"https://youtu.be/{VIDEO_ID}"

    client.post(save, {"page": "nowhere", "video_url": f"https://youtu.be/{VIDEO_ID}"})
    assert not PageHelpVideo.objects.filter(page="nowhere").exists()

    client.post(save, {"page": "dashboard", "video_url": ""})
    assert not PageHelpVideo.objects.exists()


@pytest.mark.django_db
def test_a_customer_cannot_see_or_change_the_videos(client):
    client.force_login(_user("customer@example.com"))

    assert client.get(reverse("onboarding_staff:help_videos")).status_code == 403
    client.post(reverse("onboarding_staff:help_video_save"), {"page": "dashboard", "video_url": "https://youtu.be/x"})
    assert not PageHelpVideo.objects.exists()


def _banner(tab, user):
    request = RequestFactory().get("/")
    request.user = user
    return render_to_string("composer/partials/_ideas_hero.html", {"tab": tab}, request=request)


@pytest.mark.django_db
def test_the_banner_plays_its_page_video_in_the_privacy_enhanced_player():
    PageHelpVideo.objects.create(page="calendar", video_url=f"https://www.youtube.com/watch?v={VIDEO_ID}")

    calendar = _banner("calendar", AnonymousUser())
    assert f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}?" in calendar
    assert '@click="video = true"' in calendar

    dashboard = _banner("dashboard", AnonymousUser())
    assert "youtube-nocookie" not in dashboard
    assert '@click="video = true"' not in dashboard


@pytest.mark.django_db
def test_without_a_video_the_product_offers_no_way_into_the_administrative_area():
    link = reverse("onboarding_staff:help_videos")

    assert link not in _banner("tasks", _user("op@example.com", staff=True))
    assert link not in _banner("tasks", _user("customer@example.com"))


@pytest.mark.django_db
@pytest.mark.parametrize("page", [p for p in PageHelpVideo.Page.values if p != "ideas"])
def test_every_page_with_a_video_slot_has_its_own_banner(page):
    """The operator's list of pages and the banner's own texts stay in step: a
    page added to one and not the other would fall back to the ideas banner."""
    with translation.override("en"):
        assert "Never run out of ideas" in _banner("ideas", AnonymousUser())
        assert "Never run out of ideas" not in _banner(page, AnonymousUser())
