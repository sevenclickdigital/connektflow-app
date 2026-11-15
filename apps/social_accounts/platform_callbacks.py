"""Callbacks a platform sends about a person's grant, not about their content.

Meta calls these when someone removes the app from their account
("uninstall", Threads only here) or asks for their data to be deleted. Both
arrive as a POST with one form field, ``signed_request``:
``<signature>.<payload>``, each part base64url, the signature an HMAC-SHA256
of the encoded payload keyed with the app secret. The payload's ``user_id`` is
the person: for Threads the connected account itself, for the Facebook app the
Facebook user who granted the Pages and Instagram profiles.

Public and CSRF-exempt by necessity; the signature is the only authentication.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.credentials.models import resolve_app_secret, resolve_app_secrets

from .models import DataDeletionRequest, SocialAccount
from .services import remove_account_data

logger = logging.getLogger(__name__)

THREADS = ("threads",)
# One Meta app serves both, and its data deletion request names the Facebook
# user who granted the connections rather than any one of them.
FACEBOOK_APP = ("facebook", "instagram")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parse_signed_request(raw: str):
    """Split a signed request into ``(payload, signature, signed_bytes)``.

    Returns ``None`` when it is not a well-formed HMAC-SHA256 signed request.
    Nothing here is trusted yet: the caller still has to check the signature.
    """
    encoded_signature, separator, encoded_payload = (raw or "").partition(".")
    if not separator or not encoded_signature or not encoded_payload:
        return None
    try:
        signature = _b64url_decode(encoded_signature)
        payload = json.loads(_b64url_decode(encoded_payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        return None
    return payload, signature, encoded_payload.encode()


def _signed_with(secret: str, signature: bytes, signed_bytes: bytes) -> bool:
    if not secret:
        return False
    expected = hmac.new(secret.encode(), signed_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(expected, signature)


def _accounts_named_by(request, platforms, id_field):
    """Authenticate the callback and return the accounts it is about.

    ``id_field`` is where a connection of these platforms keeps the id the
    signed request carries. Returns ``(accounts, error_response)``.

    The lookup crosses workspaces on purpose: the person acted on the
    platform, which knows nothing of our tenants, and every connection of
    theirs is affected. An account counts only when the request is signed with
    its own organization's app secret, so one organization's app cannot touch
    another's connections.
    """
    parsed = _parse_signed_request(request.POST.get("signed_request", ""))
    if parsed is None:
        return [], HttpResponseBadRequest("Malformed signed_request")
    payload, signature, signed_bytes = parsed

    if not any(_signed_with(secret, signature, signed_bytes) for secret in resolve_app_secrets(*platforms)):
        logger.warning("%s callback rejected: signature matches no configured app secret", platforms[0])
        return [], HttpResponseForbidden("Invalid signature")

    # An empty id must match nothing: it is also the value of every
    # connection whose granting user was never recorded.
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return [], None
    candidates = SocialAccount.objects.filter(platform__in=platforms, **{id_field: user_id}).select_related("workspace")
    accounts = [
        account
        for account in candidates
        if _signed_with(
            resolve_app_secret(account.platform, account.workspace.organization_id), signature, signed_bytes
        )
    ]
    return accounts, None


def _delete_and_confirm(request, platforms, id_field):
    """Remove what the request names and answer the way Meta requires.

    That is a JSON body with a confirmation code and a URL where the person
    can read the status of the request. Deletion happens here, before
    answering, so that status is always "done". A user we hold nothing for
    still gets a code: the answer to them is the same.
    """
    accounts, error = _accounts_named_by(request, platforms, id_field)
    if error:
        return error
    code = secrets.token_hex(8).upper()
    with transaction.atomic():
        for account in accounts:
            remove_account_data(account)
        DataDeletionRequest.objects.create(
            confirmation_code=code,
            platform=platforms[0],
            accounts_removed=len(accounts),
        )
    logger.info("%s data deletion callback %s: %d connection(s) removed", platforms[0], code, len(accounts))
    return JsonResponse(
        {
            "url": f"{settings.APP_URL.rstrip('/')}{reverse('data_deletion')}?code={code}",
            "confirmation_code": code,
        }
    )


@csrf_exempt
@require_POST
def threads_deauthorize(request):
    """The person removed the app in Threads: the connection no longer works.

    The account stays, marked as needing a reconnect, so the workspace sees why
    its posts stopped instead of finding the connection gone.
    """
    accounts, error = _accounts_named_by(request, THREADS, "account_platform_id")
    if error:
        return error
    for account in accounts:
        account.connection_status = SocialAccount.ConnectionStatus.DISCONNECTED
        account.last_error = _("Access was removed in Threads. Reconnect the account to keep publishing.")
        account.save(update_fields=["connection_status", "last_error", "updated_at"])
    logger.info("Threads deauthorize callback: %d connection(s) marked disconnected", len(accounts))
    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def threads_data_deletion(request):
    """Someone asked Threads for their data to be deleted."""
    return _delete_and_confirm(request, THREADS, "account_platform_id")


@csrf_exempt
@require_POST
def facebook_data_deletion(request):
    """Someone asked Facebook for the data this app holds on them to be deleted.

    Removes every Page and Instagram connection that person granted. A
    connection made before the granting user was recorded cannot be found
    this way until it is reconnected.
    """
    return _delete_and_confirm(request, FACEBOOK_APP, "granted_by_platform_user_id")
