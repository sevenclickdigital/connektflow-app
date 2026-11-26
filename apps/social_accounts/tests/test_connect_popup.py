"""Connecting from a popup, and the guide shown before the platform's window.

The popup is only a different place for the same OAuth round trip to run, so
what matters is where it ends: never with one of our pages rendered inside the
small window, and with nothing changed for a browser that did not ask for it.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.social_accounts.catalog import connect_flow, connect_guide
from apps.social_accounts.views import OAUTH_POPUP_KEY, OAUTH_SESSION_KEY, _sign_state
from apps.workspaces.models import Workspace
from providers.types import AccountProfile, OAuthTokens

LINKEDIN_ENV = {"linkedin_personal": {"client_id": "id", "client_secret": "secret"}}


@pytest.fixture(autouse=True)
def _english_responses(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Popup WS", organization=organization)


@pytest.fixture
def manager(client, user, organization, workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    client.force_login(user)
    return client


def _provider(auth_url="https://www.linkedin.com/oauth/v2/authorization?x=1"):
    provider = MagicMock()
    provider.get_auth_url.return_value = auth_url
    provider.exchange_code.return_value = OAuthTokens(access_token="token")
    provider.get_profile.return_value = AccountProfile(platform_id="p-1", name="Someone")
    return provider


def _connect(client, workspace, **data):
    url = reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})
    with patch("apps.social_accounts.views._get_provider_for_platform", return_value=_provider()):
        return client.post(url, {"platform": "linkedin_personal", **data})


def _callback(client, workspace, user):
    state = _sign_state(workspace.id, "linkedin_personal", user.id, "nonce")
    session = client.session
    session[OAUTH_SESSION_KEY] = {"nonce": "nonce"}
    session.save()
    url = reverse("social_accounts:oauth_callback", kwargs={"platform": "linkedin_personal"})
    with (
        patch("apps.social_accounts.views._get_provider_for_platform", return_value=_provider()),
        patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
    ):
        return client.get(url, {"code": "abc", "state": state})


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=LINKEDIN_ENV)
def test_a_popup_connect_still_goes_to_the_platform(manager, workspace):
    response = _connect(manager, workspace, popup="1")

    assert response.status_code == 302
    assert response.url.startswith("https://www.linkedin.com/")
    assert manager.session[OAUTH_POPUP_KEY] is True


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=LINKEDIN_ENV)
def test_the_callback_in_a_popup_hands_the_flow_back_instead_of_rendering_the_app(manager, workspace, user):
    _connect(manager, workspace, popup="1")

    response = _callback(manager, workspace, user)

    assert response.status_code == 200
    body = response.content.decode()
    here = reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})
    assert f'"{here}"' in body
    assert "cf-social-connect" in body
    assert OAUTH_POPUP_KEY not in manager.session
    assert workspace.social_accounts.filter(platform="linkedin_personal").exists()


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=LINKEDIN_ENV)
def test_without_the_popup_the_callback_redirects_as_before(manager, workspace, user):
    _connect(manager, workspace)

    response = _callback(manager, workspace, user)

    assert response.status_code == 302
    assert response.url == reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=LINKEDIN_ENV)
def test_a_later_plain_connect_clears_a_popup_left_open(manager, workspace, user):
    """Someone closes the popup half-way, then connects on a phone: that
    round trip must come back as a normal redirect."""
    _connect(manager, workspace, popup="1")
    _connect(manager, workspace)

    assert OAUTH_POPUP_KEY not in manager.session
    assert _callback(manager, workspace, user).status_code == 302


def test_a_refused_popup_connect_is_handed_back_too(manager, workspace):
    """The form posted into the popup is refused before any platform is
    involved; the error belongs on the page, not in the small window."""
    url = reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})

    response = manager.post(url, {"platform": "nope", "popup": "1"})

    assert response.status_code == 200
    assert f'"{url}"' in response.content.decode()
    assert manager.get(url).content.decode().count("This platform is not available.") == 1


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=LINKEDIN_ENV)
def test_the_page_offers_the_guide_and_posts_the_connect_into_the_popup(manager, workspace):
    page = manager.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()

    assert "Connect LinkedIn (Personal Profile)" in page
    assert "Sign in with LinkedIn" in page
    assert "data-oauth-popup" in page
    assert "guide = 'linkedin_personal'" in page


def test_mastodon_opens_the_guide_first_and_its_form_inside_the_same_modal(manager, workspace):
    page = manager.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()

    assert "guide = 'mastodon'" in page
    assert "Mastodon is a social network similar to X" in page
    guide = page[page.index("connect-guide-title-mastodon") :]
    guide = guide[: guide.index("</form>")]
    assert "step = 'form'" in guide
    connect_url = reverse("social_accounts:connect_mastodon", kwargs={"workspace_id": workspace.id})
    assert f'hx-post="{connect_url}"' in guide
    assert 'name="instance_url" value="mastodon.social"' in guide
    assert ':disabled="!other"' in guide
    assert "data-oauth-popup" in guide
    assert 'href="https://joinmastodon.org/pt-BR" target="_blank" rel="noopener noreferrer"' in guide


def _htmx_post(client, url, data):
    return client.post(url, data, HTTP_HX_REQUEST="true")


def test_a_refused_form_connect_comes_back_inside_the_modal(manager, workspace):
    url = reverse("social_accounts:connect_mastodon", kwargs={"workspace_id": workspace.id})

    response = _htmx_post(manager, url, {"instance_url": ""})

    body = response.content.decode()
    assert response.status_code == 200
    assert "Instance URL is required." in body
    assert f'hx-post="{url}"' in body
    assert "<html" not in body


def test_mastodon_from_the_modal_goes_on_to_its_server(manager, workspace):
    from apps.social_accounts.models import MastodonAppRegistration

    MastodonAppRegistration.objects.create(
        instance_url="https://mastodon.example", client_id="cid", client_secret="secret"
    )
    url = reverse("social_accounts:connect_mastodon", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._is_safe_url", return_value=True):
        response = _htmx_post(manager, url, {"instance_url": "@someone@mastodon.example"})

    assert response.status_code == 204
    assert response["HX-Redirect"].startswith("https://mastodon.example/oauth/authorize?")


def test_mastodon_in_a_popup_hands_its_server_to_the_window_and_finishes_there(manager, workspace, user):
    import json

    from apps.social_accounts.models import MastodonAppRegistration

    MastodonAppRegistration.objects.create(
        instance_url="https://mastodon.example", client_id="cid", client_secret="secret"
    )
    url = reverse("social_accounts:connect_mastodon", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._is_safe_url", return_value=True):
        response = _htmx_post(manager, url, {"instance_url": "mastodon.example", "popup": "1"})

    assert response.status_code == 204
    assert "HX-Redirect" not in response
    target = json.loads(response["HX-Trigger"])["openConnectPopup"]["url"]
    assert target.startswith("https://mastodon.example/oauth/authorize?")
    assert manager.session[OAUTH_POPUP_KEY] is True


def test_devto_from_the_modal_connects_and_reloads_the_list(manager, workspace):
    from apps.social_accounts.models import SocialAccount

    provider = MagicMock()
    provider.get_profile.return_value = AccountProfile(platform_id="dev-1", name="Dev Person")
    url = reverse("social_accounts:connect_devto", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
        response = _htmx_post(manager, url, {"api_key": "key"})

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})
    assert SocialAccount.objects.filter(workspace=workspace, platform="devto", account_platform_id="dev-1").exists()


def test_a_devto_key_the_platform_refuses_is_said_inside_the_modal(manager, workspace):
    provider = MagicMock()
    provider.get_profile.side_effect = RuntimeError("401")
    url = reverse("social_accounts:connect_devto", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
        response = _htmx_post(manager, url, {"api_key": "wrong"})

    assert response.status_code == 200
    assert "Failed to connect DEV.to account. Check your API key." in response.content.decode()


@pytest.mark.parametrize(
    ("data", "signed_in_as"),
    [
        ({"username": "@someone"}, "someone.bsky.social"),
        ({"username": "someone.bsky.social"}, "someone.bsky.social"),
        ({"handle": "brand.com"}, "brand.com"),
    ],
)
def test_bluesky_takes_a_bsky_social_username_or_a_full_handle(manager, workspace, data, signed_in_as):
    provider = MagicMock()
    provider.create_session.return_value = OAuthTokens(access_token="token")
    provider.get_profile.return_value = AccountProfile(platform_id="did:plc:1", name="Someone")
    provider.pds_url = "https://bsky.social"
    url = reverse("social_accounts:connect_bluesky", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
        response = _htmx_post(manager, url, {**data, "app_password": "xxxx-xxxx-xxxx-xxxx"})

    assert response.status_code == 204
    provider.create_session.assert_called_once_with(signed_in_as, "xxxx-xxxx-xxxx-xxxx")


def test_a_refused_bluesky_username_reopens_the_bsky_social_option(manager, workspace):
    provider = MagicMock()
    provider.create_session.side_effect = RuntimeError("401")
    url = reverse("social_accounts:connect_bluesky", kwargs={"workspace_id": workspace.id})

    with patch("apps.social_accounts.views._get_provider_for_platform", return_value=provider):
        response = _htmx_post(manager, url, {"username": "someone", "app_password": "wrong"})

    body = response.content.decode()
    assert "Failed to connect Bluesky account." in body
    assert "mode: 'bsky'" in body
    assert 'value="someone"' in body


def test_every_guide_has_three_steps_and_form_platforms_explain_what_they_are():
    for platform in (
        "facebook",
        "instagram",
        "instagram_login",
        "whatsapp",
        "linkedin_company",
        "google_business",
        "bluesky",
        "mastodon",
        "devto",
    ):
        guide = connect_guide(platform)
        assert len(guide["steps"]) == 3, platform
        assert all(str(title) and str(detail) for title, detail in guide["steps"]), platform
    for platform in ("bluesky", "mastodon", "devto"):
        assert str(connect_guide(platform)["intro"])
        assert connect_flow(platform) == "form"
    for platform in ("facebook", "linkedin_personal", "mastodon", "devto", "meta_ads"):
        assert connect_guide(platform)["site"].startswith("https://"), platform
    assert connect_flow("whatsapp") == "whatsapp"
    assert connect_flow("facebook") == "oauth"
