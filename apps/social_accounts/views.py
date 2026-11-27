"""Social account connection views.

Handles OAuth flows, account listing, connect/reconnect/disconnect actions.
"""

import json
import logging
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from csp.decorators import csp_update
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.common.htmx import lazy_body_urls, toast_response, wants_lazy_body
from apps.common.validators import is_safe_url as _is_safe_url
from apps.credentials.models import PlatformCredential, derive_is_configured
from apps.members.decorators import require_permission
from apps.radar.sources import source_cards
from providers.exceptions import ProviderError, QuotaExceededError

from .catalog import CATEGORIES, connect_flow, connect_guide, connect_order, platform_category, platform_description
from .error_messages import quota_connect_error
from .models import MESSAGING_ONLY_PLATFORMS, MastodonAppRegistration, PlatformVisibility, SocialAccount
from .oauth_aliases import from_url_slug, redirect_uri_from_request, to_url_slug
from .oauth_pkce import issue_pkce_verifier, pkce_kwargs
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag
from .services import remove_account_data, reorder_accounts
from .webhooks import (
    subscribe_account_webhooks,
    subscribe_account_webhooks_task,
    unsubscribe_account_webhooks,
)

logger = logging.getLogger(__name__)

OAUTH_STATE_MAX_AGE = 600  # 10 minutes
OAUTH_SESSION_KEY = "social_oauth"
# Set while an OAuth round trip runs in a popup opened by the Social Accounts page.
OAUTH_POPUP_KEY = "social_oauth_popup"


class AccountLimitReached(PermissionDenied):
    """The workspace is at its per-platform cap for this platform.

    A PermissionDenied so that it keeps passing through the callback's
    ``except (BadSignature, PermissionDenied): raise`` guard instead of being
    absorbed by the blanket handler's "please try again" — retrying is the one
    thing that cannot help. Its own class because the surrounding raises are
    authorization failures that should end on a 403, while this is a product
    limit whose whole value is the sentence explaining it.
    """


def _get_visible_platform_choices():
    """Return PlatformCredential.Platform.choices filtered to visible platforms.

    Platforms without a PlatformVisibility row default to visible.
    """
    return PlatformVisibility.visible_choices()


def _get_configured_platforms(org_id):
    """Return set of platform names that have credentials configured.

    With ``org_id=None`` only the site-wide sources count (the environment and
    the platforms that need no app credentials): what the administrative area
    shows, outside any organization.
    """
    from providers import PROVIDER_REGISTRY
    from providers.types import AuthType

    configured = (
        set(PlatformCredential.objects.for_org(org_id).filter(is_configured=True).values_list("platform", flat=True))
        if org_id
        else set()
    )
    env_creds = getattr(settings, "PLATFORM_CREDENTIALS_FROM_ENV", {})
    for platform, creds in env_creds.items():
        # Same completeness rule the credential resolver applies, so the grid can
        # never offer a Connect button that resolution will reject. A truthiness
        # check here would let a half-filled pair (app id set, secret missing)
        # render as connectable, walk the user through the platform's consent
        # screen, and only fail at token exchange with a generic error.
        if derive_is_configured(platform, creds):
            configured.add(platform)

    # Session-auth platforms (e.g. Bluesky) don't need app-level credentials —
    # the user supplies their own handle + app password at connect time.
    # Instance-OAuth platforms (e.g. Mastodon) don't need them either —
    # we register a per-instance OAuth app on first connect and persist it in
    # MastodonAppRegistration.
    for platform, provider_cls in PROVIDER_REGISTRY.items():
        if provider_cls().auth_type in (AuthType.SESSION, AuthType.INSTANCE_OAUTH):
            configured.add(platform)

    return configured


def _build_redirect_uri(request, platform):
    """Build the OAuth callback URL.

    Platforms with an entry in ``PLATFORM_TO_URL_ALIAS`` (currently only
    TikTok → ``social1``) use the opaque slug in the URL path so the
    redirect URI doesn't contain the platform brand name. The signed
    OAuth state still carries the real platform identifier.
    """
    from django.urls import reverse

    url_slug = to_url_slug(platform)
    return request.build_absolute_uri(reverse("social_accounts:oauth_callback", kwargs={"platform": url_slug}))


def _sign_state(workspace_id, platform, user_id, nonce):
    """Create a signed OAuth state parameter."""
    return signing.dumps(
        {
            "workspace_id": str(workspace_id),
            "platform": platform,
            "user_id": str(user_id),
            "nonce": nonce,
        },
        salt="social-oauth-state",
    )


def _unsign_state(state_str):
    """Verify and decode the OAuth state parameter."""
    return signing.loads(
        state_str,
        salt="social-oauth-state",
        max_age=OAUTH_STATE_MAX_AGE,
    )


def _normalize_mastodon_instance_url(raw):
    """Normalize user-supplied Mastodon instance input to `scheme://host[:port]`.

    Accepts: bare hosts (`mastodon.social`), URLs with paths (`https://mastodon.social/@user`),
    fediverse handles (`@user@mastodon.social`, `user@mastodon.social`), and values with
    extra whitespace or trailing slashes. Defaults the scheme to https when missing.
    Returns an empty string when the input has no host.
    """
    value = (raw or "").strip()
    if not value:
        return ""

    # Fediverse handle form: `@user@host` or `user@host`. If there's exactly one '@'
    # and no scheme, treat it as a handle and extract the host. Two '@'s means a
    # leading '@' plus user@host.
    if "://" not in value and "@" in value:
        parts = value.lstrip("@").split("@")
        if len(parts) == 2 and parts[1]:
            value = parts[1]

    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    if not parts.netloc:
        return ""
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"


def _resolve_mastodon_extra_creds(session_data):
    """Resolve Mastodon instance-specific credentials from the OAuth session.

    Returns a dict suitable for `_get_provider_for_platform(**extra_creds)`
    containing `instance_url`, and `client_id`/`client_secret` when a matching
    `MastodonAppRegistration` exists. Empty dict when no instance_url is set.
    """
    extra_creds: dict = {}
    instance_url = (session_data or {}).get("instance_url", "")
    if not instance_url:
        return extra_creds

    extra_creds["instance_url"] = instance_url
    try:
        reg = MastodonAppRegistration.objects.get(instance_url=instance_url)
        extra_creds["client_id"] = reg.client_id
        extra_creds["client_secret"] = reg.client_secret
    except MastodonAppRegistration.DoesNotExist:
        pass
    return extra_creds


# ------------------------------------------------------------------
# Account List
# ------------------------------------------------------------------


# The Social Accounts page loads Meta's JS SDK for WhatsApp Embedded Signup.
# The SDK also fetches the app's configuration from connect.facebook.net, so
# that host is a connect source as well as a script source.
_META_SDK_CSP = {
    "SCRIPT_SRC": "https://connect.facebook.net",
    "FRAME_SRC": "https://www.facebook.com https://web.facebook.com https://staticxx.facebook.com",
    "CONNECT_SRC": "https://connect.facebook.net https://www.facebook.com https://graph.facebook.com",
}


def _hosts_meta_sdk(view):
    """Let a page run Meta's JS SDK and hear back from the popup it opens.

    Embedded Signup answers through ``window.opener``: the authorization code
    and the chosen WhatsApp account are sent from Meta's popup to the page that
    opened it. The site-wide ``Cross-Origin-Opener-Policy: same-origin`` cuts
    that link, so the popup finishes and the page never hears of it. These
    pages keep the link to the popups they open themselves and nothing else.
    """

    @csp_update(**_META_SDK_CSP)
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        return response

    return wrapped


def _overview_context(request, workspace_id):
    """Everything the Social Accounts page shows: the workspace's accounts,
    the platforms it can still connect, and the opportunity sources."""
    accounts = list(
        SocialAccount.objects.for_workspace(workspace_id).prefetch_related("posting_slots").in_display_order()
    )
    configured_platforms = _get_configured_platforms(request.org.id)

    accounts_by_platform: dict[str, list] = {}
    for account in accounts:
        accounts_by_platform.setdefault(account.platform, []).append(account)
    # A platform with room for another account is offered in the connect grid;
    # one at its cap shows only as its account cards.
    available_platforms = [
        {
            "value": value,
            "label": label,
            "description": platform_description(value),
            "category": platform_category(value),
            "guide": connect_guide(value),
            "flow": connect_flow(value),
            "configured": value in configured_platforms,
            "has_accounts": value in accounts_by_platform,
        }
        for value, label in _get_visible_platform_choices()
        if len(accounts_by_platform.get(value, [])) < settings.MAX_ACCOUNTS_PER_PLATFORM
    ]
    # Ones the user can connect right now first, in the catalogue's order; unconfigured ones trail.
    available_platforms.sort(key=lambda platform: (not platform["configured"], connect_order(platform["value"])))
    problem_count = sum(1 for a in accounts if a.health == "error")
    ad_platforms = _ads_cards(request, workspace_id)
    connected_count = len(accounts) - problem_count
    available_count = len(available_platforms)
    for card in ad_platforms:
        if card["connection"] is None:
            available_count += 1
        elif card["connection"].status == "error":
            problem_count += 1
        else:
            connected_count += 1
    return {
        "accounts": accounts,
        "workspace_id": workspace_id,
        "configured_platforms": configured_platforms,
        "available_platforms": available_platforms,
        "categories": CATEGORIES,
        "stats": {
            "connected": connected_count,
            "available": available_count,
            "problems": problem_count,
            "total": len(accounts) + len(available_platforms) + len(ad_platforms),
        },
        "ad_platforms": ad_platforms,
        "has_ad_connections": any(card["connection"] for card in ad_platforms),
        "opportunity_cards": source_cards(workspace_id),
        "whatsapp_signup": _whatsapp_signup_config(request.org.id, available_platforms),
    }


def _ads_cards(request, workspace_id) -> list[dict]:
    """The ad platform cards (Meta Ads, Pinterest Ads): an ad account is not a
    ``SocialAccount``, so each has its own connection (``apps.ads``).

    ``connection`` is None while the workspace has not signed in; ``configured``
    says whether the platform's app is set up for the Connect button to work.
    """
    from django.urls import reverse

    from apps.ads import pinterest as pinterest_ads
    from apps.ads import services as meta_ads

    def card(key, label, description, service, tile, prefix, consequence):
        def url(name):
            return reverse(f"ads:{prefix}{name}", kwargs={"workspace_id": workspace_id})

        return {
            "key": key,
            "label": label,
            "description": description,
            "tile": tile,
            "connection": service.get_connection(workspace_id),
            "configured": service.is_configured(request.org.id),
            "open_url": url("index"),
            "disconnect_url": url("disconnect"),
            "consequence": consequence,
            # Shaped like a row of ``available_platforms`` for _connect_guide.html.
            "platform": {
                "value": key,
                "label": label,
                "guide": connect_guide(key),
                "flow": "oauth",
                "connect_url": url("connect"),
            },
        }

    return [
        card(
            "meta_ads",
            _("Meta Ads"),
            _("Campaigns, Pixel and results"),
            meta_ads,
            "meta_ads",
            "",
            _(
                "This removes the Meta Ads sign-in from Connekt Flow. Campaigns keep running in the ad account "
                "until you stop them there."
            ),
        ),
        card(
            "pinterest_ads",
            _("Pinterest Ads"),
            _("Campaigns that promote your pins"),
            pinterest_ads,
            "pinterest",
            "pinterest_",
            _(
                "This removes the Pinterest Ads sign-in from Connekt Flow. Campaigns keep running in the ad "
                "account until you stop them there."
            ),
        ),
    ]


def _whatsapp_signup_config(org_id, available_platforms):
    """What the browser needs to open WhatsApp Embedded Signup, or None.

    Only the app id and the Embedded Signup configuration id, both public by
    design (Meta's own snippet puts them in the page); never the secret.
    """
    if not any(p["value"] == "whatsapp" and p["configured"] for p in available_platforms):
        return None
    from apps.credentials.models import resolve_platform_credentials
    from providers.facebook import BASE_URL
    from providers.whatsapp import EMBEDDED_SIGNUP_VERSION

    creds = resolve_platform_credentials("whatsapp", org_id)
    return {
        "app_id": str(creds.get("client_id") or creds.get("app_id") or ""),
        "config_id": str(creds.get("config_id") or ""),
        "version": BASE_URL.rsplit("/", 1)[-1],
        "signup_version": EMBEDDED_SIGNUP_VERSION,
        "messages": {
            "loading": _("WhatsApp is still loading. Try again in a moment."),
            "cancelled": _("The WhatsApp connection was cancelled."),
            "no_number": _("Pick a phone number in the WhatsApp window to finish connecting."),
            "business_app": _(
                "Numbers in use in the WhatsApp Business app can't be connected yet. Use a number that isn't on the app."
            ),
            "error": _("The WhatsApp window reported an error. Try again, or check the number in WhatsApp Manager."),
        },
    }


@_hosts_meta_sdk
@login_required
@require_permission("manage_social_accounts")
def account_list(request, workspace_id):
    """The workspace's accounts, their health, and the platforms left to connect."""
    return render(request, "social_accounts/list.html", _overview_context(request, workspace_id))


# ------------------------------------------------------------------
# Connect Platform (OAuth redirect)
# ------------------------------------------------------------------


def finish_in_popup(view):
    """Let an OAuth round trip run inside a small window opened by the page.

    The Social Accounts page may open the platform's sign-in in a popup and
    post the connect form into it with ``popup=1``. Everything then happens in
    that window exactly as it would in the tab, until the flow would land back
    on one of our own pages: instead of rendering the app inside the popup,
    the window is told where the flow continues, hands that to the page that
    opened it and closes (``connect_done.html``). A redirect that leaves the
    site, towards the platform, goes through untouched.

    Without ``popup=1`` nothing changes, which is what a phone or a blocked
    popup gets.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "POST":
            if request.POST.get("popup") == "1":
                request.session[OAUTH_POPUP_KEY] = True
            else:
                request.session.pop(OAUTH_POPUP_KEY, None)
        in_popup = request.session.get(OAUTH_POPUP_KEY, False)
        response = view(request, *args, **kwargs)
        location = response.get("Location", "") if 300 <= response.status_code < 400 else ""
        if in_popup and location.startswith("/") and not location.startswith("//"):
            request.session.pop(OAUTH_POPUP_KEY, None)
            return render(request, "social_accounts/connect_done.html", {"next_url": location})
        return response

    return wrapped


@_hosts_meta_sdk
@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
@finish_in_popup
def connect_platform(request, workspace_id):
    """GET: the Social Accounts page. POST: initiate the OAuth flow."""
    if request.method == "GET":
        # Connecting happens on the Social Accounts page; this URL stays as the
        # form target and as the place errors redirect back to.
        return render(request, "social_accounts/list.html", _overview_context(request, workspace_id))

    configured_platforms = _get_configured_platforms(request.org.id)
    visible_platform_choices = _get_visible_platform_choices()

    # POST: initiate OAuth
    platform = request.POST.get("platform", "").strip()
    if platform not in dict(visible_platform_choices):
        messages.error(request, _("This platform is not available."))
        return redirect("social_accounts:connect", workspace_id=workspace_id)

    if platform not in configured_platforms:
        messages.error(
            request,
            _("Platform credentials for %(platform)s are not configured. Please contact your administrator.")
            % {"platform": platform},
        )
        return redirect("social_accounts:connect", workspace_id=workspace_id)

    # Special auth flows
    if platform == PlatformCredential.Platform.BLUESKY:
        return redirect("social_accounts:connect_bluesky", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.MASTODON:
        return redirect("social_accounts:connect_mastodon", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.DEVTO:
        return redirect("social_accounts:connect_devto", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.WHATSAPP:
        # Connected from Meta's popup on the Social Accounts page, never by redirect.
        return redirect("social_accounts:list", workspace_id=workspace_id)

    # Standard OAuth flow
    provider = _get_provider_for_platform(platform, request.org.id)
    apply_analytics_scope_flag(provider, platform)
    nonce = secrets.token_urlsafe(32)
    state = _sign_state(workspace_id, platform, request.user.id, nonce)

    # PKCE verifier (e.g. TikTok); round-trips via the session alongside the nonce.
    code_verifier = issue_pkce_verifier(provider)

    # Store nonce in session to prevent replay
    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": platform,
        "code_verifier": code_verifier,
    }

    redirect_uri = _build_redirect_uri(request, platform)
    auth_url = provider.get_auth_url(redirect_uri, state, **pkce_kwargs(code_verifier))
    return redirect(auth_url)


# ------------------------------------------------------------------
# OAuth Callback
# ------------------------------------------------------------------


@login_required
@ratelimit(key="user", rate="20/m", block=True)
@require_GET
@finish_in_popup
def oauth_callback(request, platform):
    """Handle OAuth callback from the platform.

    ``platform`` arrives as the URL slug, which may be an alias (e.g.
    ``social1`` for TikTok). Normalise it before any platform-keyed lookup
    or comparison against the signed state.
    """
    from providers import offers_account_picker

    platform = from_url_slug(platform)
    error = request.GET.get("error")
    if error:
        error_desc = request.GET.get("error_description", error)
        messages.error(request, _("OAuth error: %(error)s") % {"error": error_desc})
        session_data = request.session.pop(OAUTH_SESSION_KEY, {})
        workspace_id = session_data.get("workspace_id")
        if workspace_id:
            return redirect("social_accounts:list", workspace_id=workspace_id)
        return redirect("dashboard")

    code = request.GET.get("code")
    state_str = request.GET.get("state")

    if not code or not state_str:
        messages.error(request, _("Missing authorization code or state parameter."))
        return redirect("dashboard")

    # Validate state
    try:
        state_data = _unsign_state(state_str)
    except signing.BadSignature:
        messages.error(request, _("Invalid or expired OAuth state. Please try again."))
        return redirect("dashboard")

    # Validate nonce from session
    session_data = request.session.pop(OAUTH_SESSION_KEY, {})
    if not session_data or session_data.get("nonce") != state_data.get("nonce"):
        messages.error(request, _("OAuth session mismatch. Please try again."))
        return redirect("dashboard")

    # Validate platform matches
    if state_data.get("platform") != platform:
        messages.error(request, _("Platform mismatch in OAuth callback."))
        return redirect("dashboard")

    # Validate user
    if str(request.user.id) != state_data.get("user_id"):
        raise PermissionDenied("OAuth state does not match current user.")

    workspace_id = state_data["workspace_id"]

    # Re-check workspace membership - user may have lost access during OAuth
    from apps.members.models import WorkspaceMembership

    ws_membership = WorkspaceMembership.objects.filter(user=request.user, workspace_id=workspace_id).first()
    if not ws_membership:
        raise PermissionDenied("You no longer have access to this workspace.")
    perms = ws_membership.effective_permissions
    if not perms.get("manage_social_accounts", False):
        raise PermissionDenied("You no longer have permission to manage social accounts.")

    try:
        # For Mastodon, we need instance-specific credentials from session + registration
        extra_creds: dict = {}
        if platform == PlatformCredential.Platform.MASTODON:
            extra_creds = _resolve_mastodon_extra_creds(session_data)

        provider = _get_provider_for_platform(platform, request.org.id, **extra_creds)
        redirect_uri = redirect_uri_from_request(request)
        tokens = provider.exchange_code(code, redirect_uri, **pkce_kwargs(session_data.get("code_verifier")))

        # Platforms where one grant reaches several connectable targets — Pages,
        # company pages, Business locations — rather than one personal profile.
        # Asked of the provider rather than matched against a list of platforms
        # kept here: the two had to agree, and a provider that grew
        # ``get_user_pages`` without being added to the list fell through to the
        # single-account path below, which connects ``[0]`` and never asks. That
        # is what Google Business did — it silently connected whichever location
        # the API listed first, with no way to pick the intended one.
        if offers_account_picker(platform):
            tokens, promoted = promote_meta_user_token(provider, platform, tokens)
            pages = provider.get_user_pages(tokens.access_token)
            granting_user_id = meta_granting_user_id(provider, platform, tokens.access_token)
            if pages:
                # Filtered by workspace only: that set is bounded by what this
                # workspace has connected, while the page list is bounded only
                # by how many Pages the login administers — passing every page
                # id back as an IN clause scales with the wrong number.
                existing_ids = set(
                    SocialAccount.objects.filter(
                        workspace_id=workspace_id,
                        platform=platform,
                    ).values_list("account_platform_id", flat=True)
                )
                for page in pages:
                    page["already_connected"] = page["id"] in existing_ids
                # Store in session for account selection
                request.session["oauth_page_select"] = {
                    "workspace_id": workspace_id,
                    "platform": platform,
                    "granting_user_id": granting_user_id,
                    "user_tokens": {
                        "access_token": tokens.access_token,
                        "refresh_token": tokens.refresh_token,
                        # None once promoted: a Page token derived from a
                        # long-lived user token does not expire. On the fallback
                        # path it is the short-lived expiry, which the account
                        # must carry to be seen as expiring at all.
                        "expires_in": None if promoted else tokens.expires_in,
                        "user_expires_at": (
                            (timezone.now() + timedelta(seconds=tokens.expires_in)).isoformat()
                            if tokens.expires_in
                            else None
                        ),
                    },
                    "pages": pages,
                }
                return redirect("social_accounts:select_account")
            else:
                if platform == PlatformCredential.Platform.LINKEDIN_COMPANY:
                    warning = _(
                        "No LinkedIn Company Pages were found for your account. "
                        "Only Company Pages you administer can be connected — "
                        "personal profiles connect via the LinkedIn (Personal) option. "
                        "If you expected to see a Page, ask the page owner to grant "
                        "you Admin access in LinkedIn \u2192 Admin tools \u2192 "
                        "Manage admins, then reconnect."
                    )
                elif platform == PlatformCredential.Platform.GOOGLE_BUSINESS:
                    warning = _(
                        "No Google Business locations were found for your account. "
                        "Only verified locations can be connected. If you expected to "
                        "see one, check that it is verified and that this Google account "
                        "has access to it in Google Business Profile → Businesses, "
                        "then reconnect."
                    )
                else:
                    if platform == PlatformCredential.Platform.INSTAGRAM:
                        warning = _(
                            "No Instagram Business accounts were found for your account. "
                            "Only Instagram Business or Creator accounts linked to a Facebook Page "
                            "can be connected through this Instagram option. If you expected to "
                            "see an account, make sure it is linked to a Page you manage, then reconnect."
                        )
                    else:
                        warning = _(
                            "No Facebook Pages were found for your account. "
                            "Only Pages can be connected — personal profiles are not "
                            "supported by the Facebook API. "
                            "If you expected to see a Page, make sure you have admin "
                            "access and try removing the app in Facebook Settings \u2192 "
                            "Business Integrations, then reconnect."
                        )
                messages.warning(request, warning)
                return redirect("social_accounts:list", workspace_id=workspace_id)

        # Standard single-account flow (non-Facebook/Instagram platforms)
        profile = provider.get_profile(tokens.access_token)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=platform,
            profile=profile,
            authorizing_user=request.user,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            instance_url=extra_creds.get("instance_url", ""),
        )
        messages.success(request, _("Connected %(name)s successfully.") % {"name": profile.name})

    except AccountLimitReached as exc:
        # Ahead of the re-raise below, which would answer a product limit with a
        # bare 403 page and drop the one sentence that explains it.
        messages.error(request, str(exc))
    except (signing.BadSignature, PermissionDenied):
        raise
    except QuotaExceededError as exc:
        # Before the blanket clause below, whose "Please try again" is the one
        # piece of advice that cannot work here: the grant is fine, the
        # platform's daily budget is spent, and every retry until the window
        # rolls over fails identically. This is what a user connecting a
        # YouTube channel at 03:00 UTC was being told to keep doing.
        logger.warning("OAuth callback hit a spent %s quota: %s", platform, exc)
        messages.error(request, quota_connect_error(exc))
    except Exception:
        logger.exception("OAuth callback failed for %s", platform)
        messages.error(
            request,
            _("Failed to connect account. Please try again."),
        )

    return redirect("social_accounts:list", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Account Selection (Facebook multi-page)
# ------------------------------------------------------------------


@login_required
def select_account(request):
    """Show page/account selection after multi-page OAuth."""
    page_data = request.session.get("oauth_page_select")
    if not page_data:
        messages.error(request, _("No accounts to select. Please start over."))
        return redirect("dashboard")

    workspace_id = page_data["workspace_id"]

    def _render_picker():
        # Resolve publishability here rather than in the template: Django
        # resolves a missing key to string_if_invalid, so `can_publish is False`
        # in markup is a different predicate from the Python one and would drift
        # from it. The template gets a plain bool it can trust.
        rows = [{**page, "can_publish": page_is_publishable(page)} for page in page_data["pages"]]
        platform = page_data["platform"]
        # How many accounts not connected yet still fit under the per-platform
        # cap; refreshing an account already connected never counts against it
        # (see _create_or_update_account). The picker stops new ticks there
        # instead of letting the POST refuse the extra rows one by one.
        connected = SocialAccount.objects.filter(workspace_id=workspace_id, platform=platform).count()
        new_publishable = sum(1 for row in rows if row["can_publish"] and not row.get("already_connected"))
        return render(
            request,
            "social_accounts/account_select.html",
            {
                "pages": rows,
                "platform": platform,
                "platform_name": SocialAccount(platform=platform).get_platform_display(),
                "workspace_id": workspace_id,
                "slots": max(0, settings.MAX_ACCOUNTS_PER_PLATFORM - connected),
                "new_publishable": new_publishable,
                "publishable_count": sum(1 for row in rows if row["can_publish"]),
                "categories": sorted({row["category"] for row in rows if row.get("category")}),
            },
        )

    if request.method == "GET":
        return _render_picker()

    # POST: create accounts for selected pages
    selected_ids = request.POST.getlist("selected_pages")
    if not selected_ids:
        messages.error(request, _("Please select at least one account."))
        return _render_picker()

    from providers.types import AccountProfile

    platform = page_data["platform"]
    user_tokens = page_data["user_tokens"]
    connected = []

    for page in page_data["pages"]:
        if page["id"] in selected_ids:
            if not page_is_publishable(page):
                # Meta reported the Page's task list and it lacks CREATE_CONTENT.
                # The template already disables these rows; this guard is what
                # stops a hand-built POST from connecting an account that would
                # fail every publish.
                messages.error(
                    request,
                    _("Could not connect %(name)s: your Facebook access cannot create content for this Page.")
                    % {"name": page["name"]},
                )
                continue
            access_token = resolve_page_account_token(page, platform, user_tokens.get("access_token", ""))
            if not access_token:
                messages.error(
                    request,
                    _("Could not connect %(name)s: the platform did not provide an account token.")
                    % {"name": page["name"]},
                )
                continue

            profile = AccountProfile(
                platform_id=page["id"],
                name=page["name"],
                handle=page.get("handle"),
                avatar_url=page.get("picture", ""),
                follower_count=page.get("followers_count", 0),
            )
            try:
                _create_or_update_account(
                    workspace_id=workspace_id,
                    platform=platform,
                    profile=profile,
                    access_token=access_token,
                    refresh_token=user_tokens.get("refresh_token"),
                    expires_in=user_tokens.get("expires_in"),
                    facebook_user_access_token=user_tokens.get("access_token", "") if platform == "instagram" else None,
                    facebook_user_token_expires_at=user_tokens.get("user_expires_at"),
                    granted_by_platform_user_id=page_data.get("granting_user_id", ""),
                    # Instagram-via-Facebook receives its webhooks through the
                    # linked Page, so remember which Page to subscribe.
                    webhook_target_id=page.get("page_id", ""),
                    messaging_access=False
                    if platform == "instagram" and not page.get("access_token")
                    else page.get("can_message"),
                )
            except AccountLimitReached as exc:
                # Picking several Pages at once can cross the per-platform cap
                # part-way through. Connect what fits and name what didn't,
                # rather than discarding the whole selection over the last row.
                messages.error(
                    request,
                    _("Could not connect %(name)s: %(reason)s") % {"name": page["name"], "reason": exc},
                )
                continue
            connected.append(page["name"])

    request.session.pop("oauth_page_select", None)

    if connected:
        names = ", ".join(connected)
        messages.success(request, _("Connected: %(names)s") % {"names": names})

    return redirect("social_accounts:list", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Connect forms of our own (Bluesky, Mastodon, DEV.to)
# ------------------------------------------------------------------


def _connect_form_refused(request, workspace_id, platform, error, values=None):
    """Answer a refused Bluesky/Mastodon/DEV.to connect where the form was sent from.

    From the connect guide (HTMX) the form comes back inside the modal with the
    error and what was typed; from the platform's own page, the page again with
    the error as a message.
    """
    if request.htmx:
        return render(
            request,
            "social_accounts/partials/_connect_form.html",
            {"workspace_id": workspace_id, "platform": platform, "error": error, "values": values or {}},
        )
    messages.error(request, error)
    return render(request, f"social_accounts/{platform}_connect.html", {"workspace_id": workspace_id})


def _connect_form_redirect(request, url):
    """Leave the connect form for ``url``; from the modal HTMX has to do it.

    When the page opened a popup for the platform's sign-in (``popup=1``, see
    _oauth_popup.html), the address is handed to the page to load into it.
    """
    if request.htmx and request.POST.get("popup") == "1":
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"openConnectPopup": {"url": url}})})
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Redirect": url})
    return redirect(url)


# ------------------------------------------------------------------
# Bluesky Connect (session-based, no OAuth)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
def connect_bluesky(request, workspace_id):
    """Connect a Bluesky account via handle + app password."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/bluesky_connect.html",
            {"workspace_id": workspace_id},
        )

    # The connect guide sends either a bsky.social username or a full handle.
    username = request.POST.get("username", "").strip().lstrip("@")
    handle = request.POST.get("handle", "").strip().lstrip("@")
    typed = {"username": username} if username else {"handle": handle}
    if username:
        handle = username if username.endswith(".bsky.social") else f"{username}.bsky.social"
    app_password = request.POST.get("app_password", "").strip()

    if not handle or not app_password:
        return _connect_form_refused(
            request, workspace_id, "bluesky", _("Handle and app password are required."), typed
        )

    try:
        provider = _get_provider_for_platform(PlatformCredential.Platform.BLUESKY, request.org.id)
        tokens = provider.create_session(handle, app_password)
        profile = provider.get_profile(tokens.access_token)

        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.BLUESKY,
            profile=profile,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            instance_url=provider.pds_url,
        )
        messages.success(request, _("Connected %(name)s on Bluesky.") % {"name": profile.name})

    except AccountLimitReached as exc:
        # The credentials were fine, so the generic clause's advice to re-check
        # them would send the user hunting for a problem that isn't there.
        return _connect_form_refused(request, workspace_id, "bluesky", str(exc), typed)
    except Exception:
        logger.exception("Bluesky connection failed")
        return _connect_form_refused(
            request,
            workspace_id,
            "bluesky",
            _("Failed to connect Bluesky account. Check your handle and app password."),
            typed,
        )

    return _connect_form_redirect(request, reverse("social_accounts:list", kwargs={"workspace_id": workspace_id}))


# ------------------------------------------------------------------
# DEV.to Connect (API-key based, no OAuth)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
def connect_devto(request, workspace_id):
    """Connect a DEV.to account via a personal API key."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/devto_connect.html",
            {"workspace_id": workspace_id},
        )

    api_key = request.POST.get("api_key", "").strip()
    if not api_key:
        return _connect_form_refused(request, workspace_id, "devto", _("A DEV.to API key is required."))

    try:
        provider = _get_provider_for_platform(PlatformCredential.Platform.DEVTO, request.org.id)
        profile = provider.get_profile(api_key)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.DEVTO,
            profile=profile,
            access_token=api_key,
        )
        messages.success(request, _("Connected %(name)s on DEV.to.") % {"name": profile.name})
    except AccountLimitReached as exc:
        return _connect_form_refused(request, workspace_id, "devto", str(exc))
    except Exception:
        logger.exception("DEV.to connection failed")
        return _connect_form_refused(
            request, workspace_id, "devto", _("Failed to connect DEV.to account. Check your API key.")
        )

    return _connect_form_redirect(request, reverse("social_accounts:list", kwargs={"workspace_id": workspace_id}))


# ------------------------------------------------------------------
# WhatsApp Connect (Embedded Signup)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def connect_whatsapp(request, workspace_id):
    """Finish WhatsApp Embedded Signup.

    The browser posts what Meta's popup returned: an authorization code and the
    WhatsApp Business Account and phone number the person picked. The two ids
    come from the browser, so they are only used after the token obtained from
    the code proves it reaches that number in that account.
    """
    from providers.exceptions import MessagingError, ProviderError

    code = request.POST.get("code", "").strip()
    waba_id = request.POST.get("waba_id", "").strip()
    phone_number_id = request.POST.get("phone_number_id", "").strip()
    back = redirect("social_accounts:list", workspace_id=workspace_id)

    if not code or not waba_id.isdigit() or not phone_number_id.isdigit():
        logger.warning(
            "WhatsApp signup for workspace %s came back incomplete: code=%s waba_id=%r phone_number_id=%r",
            workspace_id,
            bool(code),
            waba_id[:32],
            phone_number_id[:32],
        )
        messages.error(request, _("WhatsApp did not finish the connection. Try again."))
        return back
    if PlatformCredential.Platform.WHATSAPP not in _get_configured_platforms(request.org.id):
        messages.error(request, _("WhatsApp is not configured. Contact your administrator."))
        return back

    provider = _get_provider_for_platform(
        PlatformCredential.Platform.WHATSAPP,
        request.org.id,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
    )
    try:
        tokens = provider.exchange_code(code)
        reachable_numbers = provider.waba_phone_number_ids(tokens.access_token, waba_id)
        if phone_number_id not in reachable_numbers:
            logger.warning(
                "WhatsApp signup for workspace %s: number %s is not among the %d numbers of account %s (%s)",
                workspace_id,
                phone_number_id,
                len(reachable_numbers),
                waba_id,
                ", ".join(sorted(reachable_numbers)),
            )
            messages.error(request, _("That phone number does not belong to the WhatsApp account you authorized."))
            return back
        provider.ensure_registered(tokens.access_token)
        profile = provider.get_profile(tokens.access_token)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.WHATSAPP,
            profile=profile,
            access_token=tokens.access_token,
            expires_in=tokens.expires_in,
            webhook_target_id=waba_id,
            authorizing_user=request.user,
        )
    except AccountLimitReached as exc:
        logger.warning("WhatsApp connection for workspace %s stopped by the account limit: %s", workspace_id, exc)
        messages.error(request, str(exc))
        return back
    except MessagingError as exc:
        logger.warning("WhatsApp connection refused for workspace %s", workspace_id, exc_info=True)
        messages.error(request, str(exc))
        return back
    except ProviderError:
        logger.exception("WhatsApp connection failed for workspace %s", workspace_id)
        messages.error(
            request, _("Meta refused the WhatsApp connection. Try again, or check the number in WhatsApp Manager.")
        )
        return back

    messages.success(
        request,
        _("WhatsApp %(number)s connected. New messages arrive in the inbox.")
        % {"number": profile.handle or profile.name},
    )
    return back


# ------------------------------------------------------------------
# Mastodon Connect (instance-based OAuth)
# ------------------------------------------------------------------


@csp_update(FORM_ACTION="'self' https:")
@login_required
@require_permission("manage_social_accounts")
@finish_in_popup
def connect_mastodon(request, workspace_id):
    """Connect a Mastodon account via instance URL + OAuth."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/mastodon_connect.html",
            {"workspace_id": workspace_id},
        )

    typed = {"instance_url": request.POST.get("instance_url", "").strip()}
    instance_url = _normalize_mastodon_instance_url(typed["instance_url"])
    if not instance_url:
        return _connect_form_refused(request, workspace_id, "mastodon", _("Instance URL is required."), typed)

    # Validate against SSRF - reject private/reserved IP ranges
    if not _is_safe_url(instance_url):
        return _connect_form_refused(
            request,
            workspace_id,
            "mastodon",
            _("Invalid instance URL. Private or reserved addresses are not allowed."),
            typed,
        )

    # Check for existing app registration or create one
    try:
        registration = MastodonAppRegistration.objects.get(instance_url=instance_url)
        client_id = registration.client_id
        client_secret = registration.client_secret
    except MastodonAppRegistration.DoesNotExist:
        # Register app on this instance
        try:
            provider = _get_provider_for_platform(
                PlatformCredential.Platform.MASTODON,
                request.org.id,
                instance_url=instance_url,
            )
            redirect_uri = _build_redirect_uri(request, PlatformCredential.Platform.MASTODON)
            app_data = provider.register_app(instance_url, redirect_uri)
            registration = MastodonAppRegistration.objects.create(
                instance_url=instance_url,
                client_id=app_data["client_id"],
                client_secret=app_data["client_secret"],
            )
            client_id = app_data["client_id"]
            client_secret = app_data["client_secret"]
        except Exception:
            logger.exception("Mastodon app registration failed for %s", instance_url)
            return _connect_form_refused(
                request,
                workspace_id,
                "mastodon",
                _("Failed to register with %(url)s. Check the URL.") % {"url": instance_url},
                typed,
            )

    # Initiate OAuth
    provider = _get_provider_for_platform(
        PlatformCredential.Platform.MASTODON,
        request.org.id,
        instance_url=instance_url,
        client_id=client_id,
        client_secret=client_secret,
    )

    nonce = secrets.token_urlsafe(32)
    state = _sign_state(
        workspace_id,
        PlatformCredential.Platform.MASTODON,
        request.user.id,
        nonce,
    )

    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": PlatformCredential.Platform.MASTODON,
        "instance_url": instance_url,
    }

    redirect_uri = _build_redirect_uri(request, PlatformCredential.Platform.MASTODON)
    auth_url = provider.get_auth_url(redirect_uri, state)
    return _connect_form_redirect(request, auth_url)


# ------------------------------------------------------------------
# Reconnect
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@require_POST
@finish_in_popup
def reconnect(request, workspace_id, account_id):
    """Re-initiate OAuth for an existing account."""
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    platform = account.platform

    if platform == PlatformCredential.Platform.BLUESKY:
        return redirect("social_accounts:connect_bluesky", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.MASTODON:
        return redirect("social_accounts:connect_mastodon", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.DEVTO:
        return redirect("social_accounts:connect_devto", workspace_id=workspace_id)

    # Standard OAuth reconnect
    provider = _get_provider_for_platform(platform, request.org.id)
    apply_analytics_scope_flag(provider, platform)
    nonce = secrets.token_urlsafe(32)
    state = _sign_state(workspace_id, platform, request.user.id, nonce)
    code_verifier = issue_pkce_verifier(provider)

    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": platform,
        "code_verifier": code_verifier,
    }

    redirect_uri = _build_redirect_uri(request, platform)
    auth_url = provider.get_auth_url(redirect_uri, state, **pkce_kwargs(code_verifier))
    return redirect(auth_url)


@login_required
@require_permission("manage_social_accounts")
@require_POST
def reorder(request, workspace_id):
    """Save the order the cards were dragged into (``order``: account ids, comma-separated).

    The composer and the other channel pickers list the accounts in this order.
    """
    reorder_accounts(workspace_id, [value for value in request.POST.get("order", "").split(",") if value.strip()])
    return toast_response(tone="success", title=_("Order saved"))


@login_required
@require_permission("manage_social_accounts")
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def retry_webhooks(request, workspace_id, account_id):
    """Re-run a failed webhook subscription without a new OAuth grant.

    A subscription can fail for reasons that have nothing to do with the token —
    a transient Graph error, or a bug in what we asked for — and sending the
    user through the full OAuth dance to retry one API call is theatre. Runs
    inline rather than through ``subscribe_account_webhooks_task``: that task
    exists so connecting *several* Pages at once doesn't stack a round trip per
    Page inside the redirect the user is waiting on, which a single deliberate
    retry doesn't. Rate limited because each press is a live round trip to the
    platform on a request thread.
    """
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)

    # A dead connection cannot carry a subscription: the call would spend a
    # round trip to be rejected, then overwrite the warning with one about
    # real-time delivery when the real problem is the connection itself. Gated
    # on the same ``needs_reconnect`` the card uses to hide the retry button, so
    # this can only be reached by a stale card or a direct POST — and answers
    # both with the reconnect state rather than an unchanged card.
    if account.needs_reconnect:
        if request.headers.get("HX-Request"):
            return _render_account_card(request, account, workspace_id)
        messages.error(
            request, _("Reconnect %(label)s first — its connection isn't healthy.") % {"label": account.display_label}
        )
        return redirect("social_accounts:list", workspace_id=workspace_id)

    # An explicit press is a fresh mandate: clear the automatic retry budget so
    # a user can always get one more attempt out of a capped-out account.
    SocialAccount.objects.filter(pk=account.pk).update(webhook_retry_count=0)
    account.webhook_retry_count = 0

    subscribed = subscribe_account_webhooks(account)
    account.refresh_from_db()

    if request.headers.get("HX-Request"):
        return _render_account_card(request, account, workspace_id)

    if subscribed:
        messages.success(request, _("Real-time updates are back on for %(label)s.") % {"label": account.display_label})
    else:
        messages.error(request, account.webhook_error or _("Couldn't set up real-time updates. Please try again."))
    return redirect("social_accounts:list", workspace_id=workspace_id)


def _start_inbox_history_job(request, workspace_id, account_id, *, action, offered, task, started_message):
    """Shared body of the two history actions on an account's menu.

    Marks the job as running, so the card shows it and stops offering either
    action, and queues the work: reading or deleting a whole inbox is too
    slow for a request. An account the action is not on offer for (already
    running, nothing to do) is left alone.
    """
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    if offered(account):
        if account.needs_reconnect and action == "import":
            messages.error(
                request,
                _("Reconnect %(label)s first — its connection isn't healthy.") % {"label": account.display_label},
            )
        else:
            account.set_inbox_history_job(action, "running")
            task(str(account.id))
            messages.success(request, started_message % {"label": account.display_label})
    return redirect("social_accounts:list", workspace_id=workspace_id)


@login_required
@require_permission("manage_social_accounts")
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def import_inbox_history(request, workspace_id, account_id):
    """Bring in the comments and conversations from before the account was connected."""
    from apps.inbox.tasks import import_inbox_history as task

    return _start_inbox_history_job(
        request,
        workspace_id,
        account_id,
        action="import",
        offered=lambda account: account.can_import_inbox_history,
        task=task,
        started_message=_("Importing the history of %(label)s. This card shows when it is done."),
    )


@login_required
@require_permission("manage_social_accounts")
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def clear_inbox_history(request, workspace_id, account_id):
    """Remove from the inbox what the account held before it was connected."""
    from apps.inbox.tasks import clear_inbox_history as task

    return _start_inbox_history_job(
        request,
        workspace_id,
        account_id,
        action="clear",
        offered=lambda account: account.can_clear_inbox_history,
        task=task,
        started_message=_("Removing the old history of %(label)s. This card shows when it is done."),
    )


@login_required
@require_permission("manage_social_accounts")
@require_GET
def account_card(request, workspace_id, account_id):
    """One account's card, for the card to refresh itself while a history job runs.

    The card polls this with ``was=running``; the answer that finds the job
    finished also carries the toast that says how it ended, so the person
    learns it without watching the card.
    """
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    response = _render_account_card(request, account, workspace_id)
    job = account.inbox_history_job or {}
    if request.GET.get("was") != "running" or job.get("state") not in ("done", "failed"):
        return response
    count = job.get("count", 0)
    clearing = job.get("action") == "clear"
    if job["state"] == "failed":
        tone = "error"
        title = (
            _("The old history could not be removed. Try again.")
            if clearing
            else _("The history could not be imported. Try again.")
        )
        body = job.get("error") or account.display_label
    else:
        tone = "success"
        title = (
            ngettext("Old history removed: %(count)s message.", "Old history removed: %(count)s messages.", count)
            if clearing
            else ngettext("History imported: %(count)s message.", "History imported: %(count)s messages.", count)
        ) % {"count": count}
        body = account.display_label
    response["HX-Trigger"] = json.dumps({"showToast": {"tone": tone, "title": str(title), "body": str(body)}})
    return response


def _render_account_card(request, account, workspace_id):
    """Render one account card for an htmx ``outerHTML`` swap of ``#account-<id>``."""
    return render(
        request,
        "social_accounts/partials/_account_card.html",
        {"account": account, "workspace_id": workspace_id},
    )


# ------------------------------------------------------------------
# Disconnect
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@require_POST
def disconnect(request, workspace_id, account_id):
    """Disconnect a social account."""
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    if account.platform == "youtube":
        from .youtube_compliance import purge_connection

        name = account.account_name or account.account_handle
        purge_connection(account)
        messages.success(request, _("Disconnected %(name)s. YouTube data was deleted immediately.") % {"name": name})
        return _after_disconnect(request, workspace_id)

    # Stop the platform pushing us this account's activity before we drop the
    # token that would let us unsubscribe.
    if account.oauth_access_token:
        unsubscribe_account_webhooks(account)

    # Try to revoke token. The account's own credentials, not just the org's:
    # a Mastodon grant lives on the account's instance, under that instance's
    # app registration, and revoking without them calls an empty base URL.
    try:
        from apps.publisher.engine import _resolve_publish_credentials
        from providers import get_provider

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        if account.revocation_token:
            provider.revoke_token(account.revocation_token)
    except Exception:
        logger.warning(
            "Failed to revoke token for %s, proceeding with disconnect",
            account,
        )

    account_name = account.account_name or account.account_handle
    remove_account_data(account)

    messages.success(request, _("Disconnected %(name)s.") % {"name": account_name})
    return _after_disconnect(request, workspace_id)


@login_required
@require_permission("manage_social_accounts")
@require_http_methods(["GET", "POST"])
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def whatsapp_templates(request, workspace_id, account_id):
    """The message templates of a connected WhatsApp number: the list Meta holds, and a form to create one.

    The list is read from Meta on every visit, so it loads after the page
    (``partial=body``). The form posts over HTMX and gets itself back: with
    its errors, or empty after the template went to Meta's review, when the
    list is told to load again.
    """
    from .whatsapp_templates import MessageTemplateForm, TemplateError, create_template, list_templates

    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id, platform="whatsapp")
    context = {"account": account, "workspace_id": workspace_id}

    if request.method == "POST":
        form = MessageTemplateForm(request.POST)
        created = False
        if form.is_valid():
            try:
                create_template(account, **form.cleaned_data)
            except TemplateError as exc:
                form.add_error(None, str(exc))
            else:
                logger.info("User %s created a WhatsApp template on account %s", request.user.pk, account.id)
                created = True
                form = MessageTemplateForm()
        response = render(request, "social_accounts/partials/_whatsapp_template_form.html", {**context, "form": form})
        if created:
            response["HX-Trigger"] = json.dumps(
                {
                    "showToast": {
                        "tone": "success",
                        "title": _("Template sent for review"),
                        "body": _("WhatsApp reviews it before it can be used, usually within a day."),
                    },
                    "whatsappTemplatesChanged": True,
                }
            )
        return response

    if wants_lazy_body(request):
        try:
            context["templates"] = list_templates(account)
        except TemplateError as exc:
            context["list_error"] = str(exc)
        return render(request, "social_accounts/partials/_whatsapp_templates_list.html", context)

    context.update(form=MessageTemplateForm(), **lazy_body_urls(request))
    return render(request, "social_accounts/whatsapp_templates.html", context)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _after_disconnect(request, workspace_id):
    """Back to the connect page when the disconnect came from there, else the list.

    A fixed choice rather than a URL from the form, so it cannot be turned into
    an open redirect.
    """
    if request.POST.get("return_to") == "connect":
        return redirect("social_accounts:connect", workspace_id=workspace_id)
    return redirect("social_accounts:list", workspace_id=workspace_id)


def meta_granting_user_id(provider, platform, access_token) -> str:
    """The Facebook user behind a Facebook Login grant, or "" when unknown.

    The connection itself is a Page or an Instagram profile; the grant belongs
    to this person, and theirs is the id Meta sends with a data deletion
    request. Failing to read it must not fail the connection: the account
    works without it, it just cannot be found by that callback, so the miss is
    logged for the operator instead.
    """
    if platform not in ("facebook", "instagram"):
        return ""
    # Same Graph call on both; each provider grew its own name for it.
    inspect = provider.inspect_token if platform == "instagram" else provider.debug_token
    try:
        user_id = (inspect(access_token) or {}).get("user_id")
    except ProviderError as exc:
        logger.warning("Could not read the granting Facebook user for a %s connection: %s", platform, exc)
        return ""
    return str(user_id) if isinstance(user_id, (str, int)) and user_id else ""


def promote_meta_user_token(provider, platform, tokens) -> tuple[object, bool]:
    """Trade a Meta authorization-code token for a long-lived one, if we can.

    Returns ``(tokens, promoted)``. ``promoted`` is what callers must key the
    stored expiry off: a Page token derived from a *long-lived* user token does
    not expire and should be stored with no expiry at all, while one derived
    from the short-lived token we fell back to expires within the hour and has
    to carry that expiry or the health and publish checks will never look at it.

    Meta's code exchange returns a *short-lived* user token, and every Page
    token derived from it inherits that lifetime — which is how an agency
    onboarding fifty Pages ends up with fifty credentials that expire before
    the first scheduled post runs. Exchanging first yields durable Page tokens.

    Best-effort on purpose. The exchange can fail for reasons that say nothing
    about whether the grant is usable (a transient Graph 5xx, an app secret the
    deployment has rotated), and the callback's error handling turns any raise
    into "Failed to connect account". A short-lived token still connects and
    still publishes today, so a failure here degrades the connection's lifetime
    rather than blocking it.
    """
    if platform not in (
        PlatformCredential.Platform.FACEBOOK,
        PlatformCredential.Platform.INSTAGRAM,
    ):
        return tokens, False
    try:
        return provider.refresh_token(tokens.access_token), True
    except Exception:
        # Deliberately broad. SocialProvider._request only converts HTTP status
        # codes into ProviderError — httpx transport failures (connect, read
        # timeout) propagate unwrapped, and a timeout is the likeliest
        # transient of all. Catching only ProviderError would let exactly the
        # case this fallback exists for reach the callback's error handler and
        # fail a connect that the short-lived token would have completed.
        logger.warning(
            "Could not exchange the %s user token for a long-lived one; continuing with the short-lived token.",
            platform,
            exc_info=True,
        )
        return tokens, False


def page_is_publishable(page: dict) -> bool:
    """Whether a Page dict from ``get_user_pages`` may be connected.

    The providers compute ``can_publish`` from Meta's per-Page task list; this
    is the single reading of that flag every caller must share. Absent means a
    provider that does not report publishability at all (LinkedIn Company), not
    "no".

    Kept beside ``resolve_page_account_token`` for the same reason that one
    exists: the interactive picker and the connection-link flow had each written
    their own copy of a page-eligibility rule once already, and diverged.
    """
    return page.get("can_publish", True) is not False


def resolve_page_account_token(page: dict, platform: str, user_access_token: str) -> str:
    """Pick the token a Page-backed account must be driven by.

    A Facebook Page needs its *own* Page token: a user token publishes under
    the wrong identity, and — because the only revoke endpoint that accepts it
    revokes the app for the whole person — makes a per-account disconnect able
    to sever every other connection they have.

    Legacy Instagram publishing/comments can fall back to the user token when
    Meta omits the Page token. Such a connection must keep DMs disabled.

    Google Business issues no per-location token at all: one grant covers every
    location it can reach, so the user token *is* the account's token rather
    than a stand-in for a missing one. Without this the picker rejected every
    location it had just offered, with "the platform did not provide an account
    token".

    Returns "" when no usable token exists, which callers must treat as "cannot
    connect this account" rather than substituting one.
    """
    token = page.get("access_token")
    if not token and platform in (
        PlatformCredential.Platform.INSTAGRAM,
        PlatformCredential.Platform.GOOGLE_BUSINESS,
    ):
        token = user_access_token
    return token or ""


@transaction.atomic
def _create_or_update_account(
    *,
    workspace_id,
    platform,
    profile,
    access_token,
    refresh_token=None,
    expires_in=None,
    instance_url="",
    webhook_target_id="",
    facebook_user_access_token=None,
    facebook_user_token_expires_at=None,
    messaging_access=None,
    authorizing_user=None,
    granted_by_platform_user_id="",
):
    """Create or update a SocialAccount from OAuth results."""
    token_expires_at = None
    if expires_in:
        token_expires_at = timezone.now() + timedelta(seconds=expires_in)

    analytics_defaults = {}
    if platform == "youtube":
        from apps.organizations.models import Organization

        org = Organization.objects.select_for_update().get(workspaces__pk=workspace_id)
        if org.is_deletion_pending:
            raise PermissionDenied("Cancel organization deletion before reconnecting YouTube.")
        analytics_defaults = {
            "youtube_authorization_checked_at": timezone.now(),
            "youtube_profile_refreshed_at": timezone.now(),
        }
        if authorizing_user is not None:
            analytics_defaults["youtube_authorized_by"] = authorizing_user
    if platform == "instagram" and facebook_user_access_token is not None:
        from django.utils.dateparse import parse_datetime

        if isinstance(facebook_user_token_expires_at, str):
            facebook_user_token_expires_at = parse_datetime(facebook_user_token_expires_at)
        analytics_defaults = {
            "oauth_facebook_user_access_token": facebook_user_access_token,
            "facebook_user_token_expires_at": facebook_user_token_expires_at,
            "analytics_status": SocialAccount.AnalyticsStatus.CHECKING,
        }

    # Cap how many accounts of one platform a workspace connects. Counted only
    # when this grant would add a row: an existing account must go through even
    # at or above the cap, or "Reconnect" would be dead for exactly the
    # workspaces most likely to need it — including every workspace that was
    # already over the cap when it was introduced.
    is_new = not SocialAccount.objects.filter(
        workspace_id=workspace_id,
        platform=platform,
        account_platform_id=profile.platform_id,
    ).exists()
    if is_new:
        connected = SocialAccount.objects.filter(workspace_id=workspace_id, platform=platform).count()
        if connected >= settings.MAX_ACCOUNTS_PER_PLATFORM:
            raise AccountLimitReached(
                ngettext(
                    "This workspace already has a %(platform)s account. Disconnect it before connecting another.",
                    "This workspace already has the maximum of %(limit)s %(platform)s accounts. "
                    "Disconnect one before connecting another.",
                    settings.MAX_ACCOUNTS_PER_PLATFORM,
                )
                % {
                    "limit": settings.MAX_ACCOUNTS_PER_PLATFORM,
                    "platform": SocialAccount(platform=platform).get_platform_display(),
                }
            )

    defaults = {
        **analytics_defaults,
        "account_name": profile.name,
        "account_handle": profile.handle or "",
        "avatar_url": profile.avatar_url or "",
        "follower_count": profile.follower_count,
        "oauth_access_token": access_token,
        "oauth_refresh_token": refresh_token or "",
        "token_expires_at": token_expires_at,
        "instance_url": instance_url,
        "webhook_target_id": webhook_target_id or "",
        "messaging_access": messaging_access,
        "granted_by_platform_user_id": granted_by_platform_user_id or "",
        "connection_status": SocialAccount.ConnectionStatus.CONNECTED,
        "last_error": "",
        # Fresh OAuth grant invalidates any prior analytics-scope failure.
        "analytics_needs_reconnect": False,
        # Likewise the webhook verdict: the subscription is about to be
        # retried below, and subscribe_account_webhooks returns early
        # without recording when the provider has no webhooks at all — so
        # without this reset a stale failure would outlive the reconnect
        # that was supposed to clear it.
        "webhooks_active": None,
        "webhook_error": "",
        "webhook_needs_reconnect": False,
        "webhook_error_detail": "",
        "webhook_retry_count": 0,
    }
    account, created = SocialAccount.objects.update_or_create(
        workspace_id=workspace_id,
        platform=platform,
        account_platform_id=profile.platform_id,
        defaults=defaults,
        # A first connection starts with an empty inbox (see the field);
        # reconnecting never moves that line, in either direction.
        create_defaults={**defaults, "inbox_history_cutoff": timezone.now()},
    )

    if created and platform not in MESSAGING_ONLY_PLATFORMS:
        from apps.calendar.services import create_default_queue_and_slots

        create_default_queue_and_slots(account)

    if platform == "youtube":
        from django.db.models import F

        SocialAccount.objects.filter(pk=account.pk).update(youtube_data_generation=F("youtube_data_generation") + 1)
        account.refresh_from_db()

    subscribe_account_webhooks_task(str(account.id))

    return account
