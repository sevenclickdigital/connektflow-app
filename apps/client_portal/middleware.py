"""Keep a session opened by a magic link inside the client portal.

Redeeming a magic link logs the client in (``services.create_portal_session``)
with a real Django session. Without this middleware that session would open
everything the account can reach — the workspace screens, the client's other
organizations — so whoever holds a forwarded or leaked e-mail would hold the
whole account. The session is marked with ``PORTAL_SESSION_KEY`` and, while
the mark is there, only the portal and what it needs to render are served.
"""

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

# Session keys set by ``services.create_portal_session`` and read by
# ``decorators.portal_auth_required``.
PORTAL_SESSION_KEY = "is_portal_session"
PORTAL_WORKSPACE_KEY = "portal_workspace_id"

# Besides the portal itself: leaving (logout), the terms page the ToS
# middleware sends a new client to, the language switch, and files.
_ALWAYS_ALLOWED_PREFIXES = (
    "/portal/",
    "/accounts/logout/",
    "/accounts/accept-terms/",
    "/i18n/",
    "/health/",
)


def is_portal_session(request) -> bool:
    session = getattr(request, "session", None)
    return bool(session is not None and session.get(PORTAL_SESSION_KEY))


def clear_portal_flags(sender, request, user, **kwargs):
    """``user_logged_in`` receiver: every login starts unconfined.

    ``login()`` keeps the session data when the same user signs in again, so
    without this a client who once used a magic link would stay confined
    after signing in with a password or Google. ``create_portal_session``
    sets the flags again after its own ``login()``.
    """
    session = getattr(request, "session", None)
    if session is None or PORTAL_SESSION_KEY not in session:
        return
    session.pop(PORTAL_SESSION_KEY, None)
    session.pop(PORTAL_WORKSPACE_KEY, None)
    # Drop the portal's own expiry so the session follows SESSION_COOKIE_AGE.
    session.set_expiry(None)


class PortalSessionConfinementMiddleware:
    """Serve a portal session only the portal.

    Any other URL is redirected to the portal home for a plain GET, and
    refused with 403 for anything else (a POST, an htmx request), where a
    redirect would be followed and its page swapped into a fragment.
    Must run after ``AuthenticationMiddleware`` and before anything that acts
    on the URL's tenant (``RBACMiddleware``).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        prefixes = list(_ALWAYS_ALLOWED_PREFIXES)
        for url in (settings.STATIC_URL, settings.MEDIA_URL):
            # A URL on another host (CDN, object storage) never reaches here.
            if url and url.startswith("/") and url != "/":
                prefixes.append(url)
        self.allowed_prefixes = tuple(prefixes)

    def __call__(self, request):
        # Cookie check first: reading the session costs a database query that a
        # request without one should not pay.
        if (
            settings.SESSION_COOKIE_NAME not in request.COOKIES
            or request.path.startswith(self.allowed_prefixes)
            or not is_portal_session(request)
        ):
            return self.get_response(request)

        is_htmx = request.headers.get("HX-Request") == "true"
        if request.method in ("GET", "HEAD") and not is_htmx:
            return redirect(reverse("client_portal:dashboard"))
        return HttpResponse(_("This page is not available from the client portal."), status=403)
