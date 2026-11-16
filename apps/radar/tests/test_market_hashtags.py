"""Market hashtags: typed by the operator per segment, enriched by a weekly
reading on Instagram, and shown to the brands that follow the segment."""

from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import DateSegment, WorkspaceDateSegment
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.radar import market_hashtags
from apps.radar.models import MarketHashtag, MarketHashtagCollector
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError


def _brand(email, *, staff=False):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now(), is_staff=staff)
    org = Organization.objects.create(name=f"Org of {email}")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, workspace


def _instagram(workspace, name="IG"):
    return SocialAccount.objects.create(
        workspace=workspace, platform="instagram", account_platform_id=name, account_name=name
    )


@pytest.fixture
def food(db):
    return DateSegment.objects.create(name="Comida de rua")


def _tags(workspace):
    return [t["tag"] for t in market_hashtags.market_hashtags_for(workspace)]


@pytest.mark.django_db
def test_a_brand_sees_the_hashtags_of_its_segments_and_the_global_ones(food):
    _user, follower = _brand("a@example.com")
    _other, bystander = _brand("b@example.com")
    WorkspaceDateSegment.objects.create(workspace=follower, segment=food)
    market_hashtags.add_hashtag(food, "#Pastel")
    market_hashtags.add_hashtag(None, "dicas")

    assert _tags(follower) == ["dicas", "pastel"]
    assert _tags(bystander) == ["dicas"]


@pytest.mark.django_db
def test_what_instagram_shows_beside_a_seed_comes_first_by_frequency(food):
    _operator, own = _brand("op@example.com", staff=True)
    _user, follower = _brand("a@example.com")
    WorkspaceDateSegment.objects.create(workspace=follower, segment=food)
    market_hashtags.add_hashtag(food, "pastel")
    collector = MarketHashtagCollector.load()
    collector.account = _instagram(own)
    collector.save()
    media = [
        {"caption": "Feira de domingo #pastel #feira #caldodecana"},
        {"caption": "Hoje tem! #Pastel #feira"},
        {"caption": "#pastel #caldodecana #feira #umavez"},
        {"caption": None},
    ]

    with mock.patch("apps.radar.instagram.hashtag_posts", return_value=media) as read:
        market_hashtags.collect()

    read.assert_called_once()
    found = {t["tag"]: t for t in market_hashtags.market_hashtags_for(follower)}
    # The seed is not counted as found, and a tag seen once is left out.
    assert [(t, found[t]["posts"], found[t]["found"]) for t in found] == [
        ("feira", 3, True),
        ("caldodecana", 2, True),
        ("pastel", 0, False),
    ]
    assert MarketHashtag.objects.get(tag="pastel").collected_at is not None
    assert MarketHashtagCollector.load().last_error == ""


@pytest.mark.django_db
def test_a_refusal_from_meta_is_recorded_and_nothing_is_lost(food):
    _operator, own = _brand("op@example.com", staff=True)
    market_hashtags.add_hashtag(food, "pastel")
    market_hashtags.add_hashtag(food, "coxinha")
    MarketHashtag.objects.create(segment=food, tag="feira", source="meta", posts=5)
    collector = MarketHashtagCollector.load()
    collector.account = _instagram(own)
    collector.save()
    refusal = APIError("(#10) permission", platform="instagram", status_code=403)

    with mock.patch("apps.radar.instagram.hashtag_posts", side_effect=refusal) as read:
        market_hashtags.collect()

    # The first refusal ends the run: the others would hear the same.
    assert read.call_count == 1
    assert "Instagram Public Content Access" in MarketHashtagCollector.load().last_error
    assert market_hashtags.collection_status()["ok"] is False
    assert MarketHashtag.objects.filter(tag="feira", source="meta", posts=5).exists()


@pytest.mark.django_db
def test_without_an_account_nothing_is_asked_of_instagram(food):
    market_hashtags.add_hashtag(food, "pastel")
    with mock.patch("apps.radar.instagram.hashtag_posts") as read:
        market_hashtags.collect()
    read.assert_not_called()


@pytest.mark.django_db
def test_the_operator_keeps_the_shelf_and_chooses_only_an_account_of_their_own(client, food):
    operator, own = _brand("op@example.com", staff=True)
    _customer, customers_brand = _brand("a@example.com")
    mine, theirs = _instagram(own, "mine"), _instagram(customers_brand, "theirs")
    client.force_login(operator)

    assert client.get(reverse("radar_staff:market_hashtags")).status_code == 200
    client.post(reverse("radar_staff:market_hashtag_add"), {"segment": food.id, "tag": "#Pastel"})
    client.post(reverse("radar_staff:market_hashtag_add"), {"tag": "duas palavras"})
    assert list(MarketHashtag.objects.values_list("tag", flat=True)) == ["pastel"]

    client.post(reverse("radar_staff:market_hashtag_account"), {"account": theirs.id})
    assert MarketHashtagCollector.load().account is None
    client.post(reverse("radar_staff:market_hashtag_account"), {"account": mine.id})
    assert MarketHashtagCollector.load().account == mine

    row = MarketHashtag.objects.get(tag="pastel")
    client.post(reverse("radar_staff:market_hashtag_delete", kwargs={"hashtag_id": row.id}))
    assert not MarketHashtag.objects.exists()


@pytest.mark.django_db
def test_a_customer_cannot_touch_the_market_hashtags(client, food):
    customer, _workspace = _brand("a@example.com")
    client.force_login(customer)

    assert client.get(reverse("radar_staff:market_hashtags")).status_code in (302, 403, 404)
    client.post(reverse("radar_staff:market_hashtag_add"), {"tag": "invasao"})
    client.post(reverse("radar_staff:market_hashtag_collect"))

    assert not MarketHashtag.objects.exists()
