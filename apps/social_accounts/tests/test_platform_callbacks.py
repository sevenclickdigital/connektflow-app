"""Threads' uninstall and data-deletion callbacks.

Both are public endpoints that change or delete a workspace's connection on
the word of a signed request, so the tests are about who may trigger what:
only a request signed with the Threads app secret, and only against the
connections of the organization that secret belongs to.
"""

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.composer.models import PlatformPost, Post
from apps.credentials.models import PlatformCredential
from apps.organizations.models import Organization
from apps.social_accounts.models import DataDeletionRequest, SocialAccount
from apps.workspaces.models import Workspace

SECRET = "threads-app-secret"
ENV_CREDENTIALS = {"threads": {"app_id": "threads-app", "app_secret": SECRET}}
THREADS_USER = "17841400000000001"

pytestmark = pytest.mark.django_db


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _signed_request(user_id=THREADS_USER, *, secret=SECRET, algorithm="HMAC-SHA256") -> str:
    payload = _b64url(json.dumps({"algorithm": algorithm, "issued_at": 1, "user_id": user_id}).encode())
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{_b64url(signature)}.{payload}"


@pytest.fixture
def workspace(organization):
    return Workspace.objects.create(name="Brand", organization=organization)


def _threads_account(workspace, user_id=THREADS_USER):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="threads",
        account_platform_id=user_id,
        account_name="Brand on Threads",
        oauth_access_token="token",
    )


def _post(client, name, signed_request):
    return client.post(reverse(name), {"signed_request": signed_request})


# ------------------------------------------------------------- authentication


@pytest.mark.parametrize("name", ["threads_deauthorize", "threads_data_deletion"])
@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_a_request_signed_with_another_secret_is_refused_and_changes_nothing(client, workspace, name):
    account = _threads_account(workspace)

    response = _post(client, name, _signed_request(secret="not-our-secret"))

    assert response.status_code == 403
    account.refresh_from_db()
    assert account.connection_status == SocialAccount.ConnectionStatus.CONNECTED
    assert not DataDeletionRequest.objects.exists()


@pytest.mark.parametrize("name", ["threads_deauthorize", "threads_data_deletion"])
@pytest.mark.parametrize("body", ["", "no-dot", "!!!.???", "."])
@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_a_malformed_request_is_refused(client, workspace, name, body):
    _threads_account(workspace)

    assert _post(client, name, body).status_code == 400
    assert SocialAccount.objects.count() == 1


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_an_unsigned_algorithm_is_refused(client, workspace):
    _threads_account(workspace)

    assert _post(client, "threads_data_deletion", _signed_request(algorithm="none")).status_code == 400
    assert SocialAccount.objects.count() == 1


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_the_callbacks_only_answer_post(client):
    assert client.get(reverse("threads_deauthorize")).status_code == 405
    assert client.get(reverse("threads_data_deletion")).status_code == 405


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV={})
def test_one_organizations_threads_app_cannot_touch_anothers_connection(client, workspace):
    """Each organization brought its own Threads app. A request signed by one
    of them names a Threads user the other organization also has connected."""
    other_org = Organization.objects.create(name="Other organization")
    other_workspace = Workspace.objects.create(name="Other brand", organization=other_org)
    PlatformCredential.objects.create(
        organization=workspace.organization,
        platform="threads",
        credentials={"app_id": "a", "app_secret": "secret-a"},
        is_configured=True,
    )
    PlatformCredential.objects.create(
        organization=other_org,
        platform="threads",
        credentials={"app_id": "b", "app_secret": "secret-b"},
        is_configured=True,
    )
    mine = _threads_account(workspace)
    theirs = _threads_account(other_workspace)

    response = _post(client, "threads_data_deletion", _signed_request(secret="secret-a"))

    assert response.status_code == 200
    assert not SocialAccount.objects.filter(id=mine.id).exists()
    assert SocialAccount.objects.filter(id=theirs.id).exists()
    assert DataDeletionRequest.objects.get().accounts_removed == 1


# ---------------------------------------------------------------- deauthorize


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_deauthorize_marks_that_users_connection_as_needing_a_reconnect(client, workspace):
    account = _threads_account(workspace)
    someone_else = _threads_account(workspace, user_id="999")

    response = _post(client, "threads_deauthorize", _signed_request())

    assert response.status_code == 200
    account.refresh_from_db()
    assert account.connection_status == SocialAccount.ConnectionStatus.DISCONNECTED
    assert account.needs_reconnect
    assert account.last_error
    someone_else.refresh_from_db()
    assert someone_else.connection_status == SocialAccount.ConnectionStatus.CONNECTED


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_deauthorize_leaves_other_platforms_with_the_same_id_alone(client, workspace):
    instagram = SocialAccount.objects.create(
        workspace=workspace, platform="instagram", account_platform_id=THREADS_USER, account_name="IG"
    )

    assert _post(client, "threads_deauthorize", _signed_request()).status_code == 200

    instagram.refresh_from_db()
    assert instagram.connection_status == SocialAccount.ConnectionStatus.CONNECTED


# -------------------------------------------------------------- data deletion


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS, APP_URL="https://app.example.com")
def test_data_deletion_removes_the_connection_and_answers_with_a_code_and_a_status_link(client, workspace, user):
    account = _threads_account(workspace)
    only_here = Post.objects.create(workspace=workspace, author=user, caption="Only on Threads")
    PlatformPost.objects.create(post=only_here, social_account=account)
    other_account = SocialAccount.objects.create(
        workspace=workspace, platform="bluesky", account_platform_id="did:1", account_name="Bluesky"
    )
    shared = Post.objects.create(workspace=workspace, author=user, caption="On both")
    PlatformPost.objects.create(post=shared, social_account=account)
    PlatformPost.objects.create(post=shared, social_account=other_account)

    response = _post(client, "threads_data_deletion", _signed_request())

    assert response.status_code == 200
    body = response.json()
    code = body["confirmation_code"]
    assert code.isalnum()
    assert body["url"] == f"https://app.example.com/exclusao-de-dados/?code={code}"
    assert not SocialAccount.objects.filter(id=account.id).exists()
    assert not Post.objects.filter(id=only_here.id).exists()
    assert Post.objects.filter(id=shared.id).exists()
    assert PlatformPost.objects.filter(post=shared).count() == 1
    record = DataDeletionRequest.objects.get(confirmation_code=code)
    assert (record.platform, record.accounts_removed) == ("threads", 1)


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=ENV_CREDENTIALS)
def test_data_deletion_for_a_user_we_hold_nothing_for_still_answers(client, workspace):
    kept = _threads_account(workspace, user_id="999")

    response = _post(client, "threads_data_deletion", _signed_request())

    assert response.status_code == 200
    assert SocialAccount.objects.filter(id=kept.id).exists()
    assert DataDeletionRequest.objects.get(confirmation_code=response.json()["confirmation_code"]).accounts_removed == 0


# ---------------------------------------------------------------- status page


def test_the_status_page_reports_a_known_request_and_admits_an_unknown_code(client):
    DataDeletionRequest.objects.create(confirmation_code="ABC123", platform="threads", accounts_removed=1)

    known = client.get(reverse("data_deletion"), {"code": "ABC123"}).content.decode()
    unknown = client.get(reverse("data_deletion"), {"code": "NOPE"}).content.decode()
    plain = client.get(reverse("data_deletion")).content.decode()

    assert "Pedido ABC123: concluído" in known
    assert "Não encontramos um pedido com o código NOPE" in unknown
    assert "Pedido" not in plain and "Não encontramos" not in plain


# ------------------------------------------- facebook app: data deletion callback

FB_SECRET = "facebook-app-secret"
FB_ENV = {
    "facebook": {"app_id": "fb-app", "app_secret": FB_SECRET},
    "instagram": {"app_id": "fb-app", "app_secret": FB_SECRET},
    "threads": {"app_id": "threads-app", "app_secret": SECRET},
}
FB_USER = "10230000000000001"


def _meta_account(workspace, platform, platform_id, granted_by=FB_USER):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id,
        account_name=f"{platform} {platform_id}",
        oauth_access_token="token",
        granted_by_platform_user_id=granted_by,
    )


@pytest.mark.parametrize("name", ["data_deletion", "data_deletion_en"])
@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=FB_ENV, APP_URL="https://app.example.com")
def test_facebook_data_deletion_removes_every_connection_that_user_granted(client, workspace, name):
    """The address saved in the Meta app is the public page's own: Facebook
    POSTs the signed request there, in either language."""
    page = _meta_account(workspace, "facebook", "page-1")
    instagram = _meta_account(workspace, "instagram", "ig-1")
    someone_elses = _meta_account(workspace, "facebook", "page-2", granted_by="999")
    unrecorded = _meta_account(workspace, "facebook", "page-3", granted_by="")

    response = _post(client, name, _signed_request(FB_USER, secret=FB_SECRET))

    assert response.status_code == 200
    code = response.json()["confirmation_code"]
    assert response.json()["url"] == f"https://app.example.com/exclusao-de-dados/?code={code}"
    assert not SocialAccount.objects.filter(id__in=[page.id, instagram.id]).exists()
    assert SocialAccount.objects.filter(id__in=[someone_elses.id, unrecorded.id]).count() == 2
    record = DataDeletionRequest.objects.get(confirmation_code=code)
    assert (record.platform, record.accounts_removed) == ("facebook", 2)


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=FB_ENV)
def test_a_request_without_a_user_id_matches_no_connection(client, workspace):
    """Every connection whose granting user was never recorded holds an empty
    id; a signed request with an empty one must not sweep them all away."""
    unrecorded = _meta_account(workspace, "facebook", "page-3", granted_by="")

    response = _post(client, "data_deletion", _signed_request("", secret=FB_SECRET))

    assert response.status_code == 200
    assert SocialAccount.objects.filter(id=unrecorded.id).exists()
    assert DataDeletionRequest.objects.get().accounts_removed == 0


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=FB_ENV)
def test_each_meta_app_secret_only_opens_its_own_callback(client, workspace):
    page = _meta_account(workspace, "facebook", "page-1")
    threads = _threads_account(workspace)

    with_threads_secret = _post(client, "data_deletion", _signed_request(FB_USER, secret=SECRET))
    with_facebook_secret = _post(client, "threads_data_deletion", _signed_request(THREADS_USER, secret=FB_SECRET))

    assert (with_threads_secret.status_code, with_facebook_secret.status_code) == (403, 403)
    assert SocialAccount.objects.filter(id__in=[page.id, threads.id]).count() == 2
    assert not DataDeletionRequest.objects.exists()


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV=FB_ENV)
def test_the_public_page_refuses_an_unsigned_post_and_other_methods(client, workspace):
    page = _meta_account(workspace, "facebook", "page-1")

    assert client.post(reverse("data_deletion"), {"signed_request": "x.y"}).status_code == 400
    assert client.post(reverse("data_deletion"), {}).status_code == 400
    assert client.delete(reverse("data_deletion")).status_code == 405
    assert client.get(reverse("data_deletion")).status_code == 200
    assert SocialAccount.objects.filter(id=page.id).exists()


# ------------------------------------------------ recording who granted a connection


def test_connecting_records_the_granting_user_and_reconnecting_updates_it(workspace):
    from apps.social_accounts.views import _create_or_update_account
    from providers.types import AccountProfile

    profile = AccountProfile(platform_id="page-1", name="Page")
    with patch("apps.social_accounts.views.subscribe_account_webhooks_task"):
        account = _create_or_update_account(
            workspace_id=workspace.id,
            platform="facebook",
            profile=profile,
            access_token="t",
            granted_by_platform_user_id="111",
        )
        assert account.granted_by_platform_user_id == "111"
        account = _create_or_update_account(
            workspace_id=workspace.id,
            platform="facebook",
            profile=profile,
            access_token="t",
            granted_by_platform_user_id="222",
        )
    assert account.granted_by_platform_user_id == "222"
    assert SocialAccount.objects.filter(platform="facebook").count() == 1


def test_the_granting_user_is_read_from_the_token_and_never_breaks_the_connection():
    from apps.social_accounts.views import meta_granting_user_id
    from providers.exceptions import APIError

    def provider(result=None, error=None):
        return MagicMock(debug_token=MagicMock(return_value=result, side_effect=error))

    assert meta_granting_user_id(provider({"user_id": "123"}), "facebook", "t") == "123"
    assert meta_granting_user_id(provider({"user_id": 123}), "instagram", "t") == "123"
    assert meta_granting_user_id(provider({}), "facebook", "t") == ""
    assert meta_granting_user_id(provider(error=APIError("nope", platform="facebook")), "facebook", "t") == ""
    other = provider({"user_id": "123"})
    assert meta_granting_user_id(other, "linkedin_company", "t") == ""
    other.debug_token.assert_not_called()
