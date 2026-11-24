import hashlib
import logging

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from apps.common.htmx import toast_response

logger = logging.getLogger(__name__)

# Paths that are rate-limited for unauthenticated POST requests (auth flows)
AUTH_RATE_LIMITED_PATHS = (
    "/accounts/login/",
    "/accounts/signup/",
    "/accounts/password/reset/",
    "/accounts/password/reset/key/",
    "/admin/login/",
)

# Rate limit: 10 POST requests per minute per IP for auth endpoints
AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW = 60  # seconds

EXEMPT_PATH_PREFIXES = (
    "/accounts/accept-terms/",
    # The page that closes the sign-in popup: the terms are then asked in the
    # window the person is actually working in.
    "/accounts/popup-done/",
    "/accounts/logout/",
    "/accounts/google/",
    "/accounts/3rdparty/",
    "/health/",
    "/static/",
    # Uploaded media, served by config/urls.py when SERVE_MEDIA is on. No view
    # backs these URLs, so a redirect here just turns every <img>/<video> on
    # the accept-terms page into a 302 — and the platforms that fetch
    # attachment URLs server-side (see config/urls.py) are anonymous anyway.
    "/media/",
    "/admin/",
)


# allauth's pages that change how the account signs in: its e-mail
# addresses, its password and the Google account linked to it (a POST to
# the Google login while signed in links the account). A takeover that
# changed any of them would keep a way into the account after it ended.
TAKEOVER_REFUSED_URL_NAMES = frozenset(
    {
        "account_email",
        "account_change_password",
        "account_set_password",
        "socialaccount_connections",
        "google_login",
    }
)


class ImpersonationGuardMiddleware:
    """Keep a takeover within its limits.

    Ends the session once its operator stops being one, and for the rest
    of the request refuses scheduling and publishing: a row that tries to
    enter the queue raises ``PublishingBlockedError``. The customer-only
    actions (``apps.accounts.impersonation.no_takeover``) raise its parent,
    ``TakeoverRefusedError``, and allauth's sign-in settings, which have no
    view of ours to decorate, are refused in ``process_view`` by URL name. Every refusal
    is answered with a message instead of an error page.

    Runs right after ``AuthenticationMiddleware`` so that no later
    middleware or view serves the request as the customer.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Cookie check first: reading the session loads it from the database
        # and marks the response ``Vary: Cookie``, which a request without a
        # session (static files, bearer API calls) should not pay for.
        if settings.SESSION_COOKIE_NAME not in request.COOKIES:
            return self.get_response(request)

        from apps.accounts import impersonation

        impersonation.end_if_operator_revoked(request)
        if not impersonation.is_impersonating(request):
            return self.get_response(request)
        with impersonation.block_publishing():
            return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.resolver_match.url_name not in TAKEOVER_REFUSED_URL_NAMES:
            return None
        from apps.accounts import impersonation

        if not impersonation.is_impersonating(request):
            return None
        return self._refusal(request, impersonation.TakeoverRefusedError())

    def process_exception(self, request, exception):
        from apps.accounts.impersonation import TakeoverRefusedError

        if not isinstance(exception, TakeoverRefusedError):
            return None
        return self._refusal(request, exception)

    @staticmethod
    def _refusal(request, exception):
        """A toast for HTMX, otherwise a message and a redirect back to the page it came from."""
        if getattr(request, "htmx", False):
            return toast_response(tone="error", title=str(exception.title), body=str(exception.body))
        messages.error(request, f"{exception.title}. {exception.body}")
        back = request.META.get("HTTP_REFERER", "")
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            back = "/"
        return redirect(back)


class OperatorAreaMiddleware:
    """Keep a team account inside the administrative area.

    A team account owns nothing (``apps.accounts.operators``), so the
    product has nothing to show it. This is what holds if a membership
    reaches one anyway: the tenant context ``RBACMiddleware`` resolved is
    dropped, and any URL outside ``/staff/`` and ``OPERATOR_NAMESPACES`` goes
    back to the area's home. Must run after ``RBACMiddleware``.

    A takeover is not an operator request (``impersonation.is_operator``),
    so it passes through as the customer.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_operator(request):
            request.org = None
            request.org_membership = None
            request.org_memberships = []
            request.workspace = None
            request.workspace_membership = None
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from apps.accounts.operators import OPERATOR_HOME, OPERATOR_NAMESPACES, STAFF_AREA_PREFIX

        match = request.resolver_match
        if match.route.startswith(STAFF_AREA_PREFIX) or not match.namespace or match.namespace in OPERATOR_NAMESPACES:
            return None
        if not self._is_operator(request):
            return None

        target = reverse(OPERATOR_HOME)
        if getattr(request, "htmx", False):
            # A plain redirect would be followed by the XHR and the whole
            # page swapped into whatever fragment asked.
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)

    @staticmethod
    def _is_operator(request):
        # Cookie check first, for the reason given in ImpersonationGuardMiddleware.
        if settings.SESSION_COOKIE_NAME not in request.COOKIES:
            return False
        from apps.accounts import impersonation

        return impersonation.is_operator(request)


class TosAcceptanceMiddleware:
    """Redirect authenticated users to the ToS acceptance page if they haven't accepted yet."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Path check first: ``request.user`` is lazy, and touching it forces a
        # session read plus a user query. On an exempt path — /static/, /media/
        # — that work buys nothing, and media is requested once per file.
        if (
            not request.path.startswith(EXEMPT_PATH_PREFIXES)
            and hasattr(request, "user")
            and request.user.is_authenticated
            and request.user.tos_accepted_at is None
        ):
            return redirect(reverse("accounts:accept_terms"))

        return self.get_response(request)


class AuthRateLimitMiddleware:
    """Rate-limit POST requests to authentication endpoints (login, admin login, signup, password reset).

    The address is ``impersonation.client_ip`` (allauth's adapter), so this
    limit and allauth's own per-IP limits agree on who the client is: the hop
    our proxy appended, or Cloudflare's CF-Connecting-IP when that hop is a
    Cloudflare edge. Never the leftmost X-Forwarded-For value, which the
    client writes itself and could rotate on every attempt to escape the
    bucket.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and any(request.path.startswith(p) for p in AUTH_RATE_LIMITED_PATHS):
            ip = self._get_client_ip(request)
            cache_key = f"auth_ratelimit:{hashlib.sha256(ip.encode()).hexdigest()}"

            # add() seeds the window once; incr() is atomic, so parallel
            # guesses cannot all read the same count and slip through together.
            cache.add(cache_key, 0, AUTH_RATE_WINDOW)
            try:
                attempts = cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, AUTH_RATE_WINDOW)
                attempts = 1
            if attempts > AUTH_RATE_LIMIT:
                if attempts == AUTH_RATE_LIMIT + 1:
                    logger.warning("Auth rate limit reached for %s on %s", ip, request.path)
                return HttpResponse(_("Too many attempts. Please wait a minute and try again."), status=429)

        return self.get_response(request)

    @staticmethod
    def _get_client_ip(request):
        from apps.accounts.impersonation import client_ip

        return client_ip(request) or ""
