"""Operator impersonation: taking over a customer's account for support.

The most dangerous feature in the product, so every rule it depends on
lives here rather than being spread across a view, a template and a
decorator:

- **Only a real operator starts one.** The ``is_staff`` check reads the
  *real* user, resolved via :func:`real_user`, never ``request.user`` —
  which during a takeover is the customer.
- **Staff never impersonate staff.** Lateral takeover between operators
  is privilege escalation with no audit value: the trail would say
  "operator A acted as operator B", and every action B's account then
  took would be deniable. Refused outright.
- **The takeover carries no operator privilege.** While impersonating,
  :func:`is_operator` is False, so the staff console and anything else
  gated on it are closed. An operator who wants to comp a workspace must
  stop impersonating first. Otherwise the audit trail would show a
  customer's account performing operator actions.
- **A takeover cannot schedule or publish.** It exists to look at what
  the customer sees and to fix their settings, not to speak in their
  name on a social network. While one is active, :func:`block_publishing`
  makes ``PlatformPost.save`` refuse a row entering ``scheduled`` — the
  one state every way of publishing goes through — with
  :class:`PublishingBlockedError`.
- **A takeover cannot take decisions that are the customer's.** Deleting
  or archiving, paying, changing who has access, changing how the account
  signs in, and speaking to third parties (inbox replies, ads, portal
  links) are refused by :func:`refuse_during_takeover` or the
  :func:`no_takeover` view decorator. Both raise
  :class:`TakeoverRefusedError`, which ``ImpersonationGuardMiddleware``
  answers with a toast (HTMX) or a message and a redirect back.
- **Every takeover is recorded**, start and end, in
  ``ImpersonationSession``.

How the session is held
-----------------------
Two keys, written on top of Django's normal auth session:

``IMPERSONATOR_SESSION_KEY``
    The real operator's primary key. Its presence *is* the "I am
    impersonating" flag — there is no separate boolean that could
    disagree with it.
``RECORD_SESSION_KEY``
    The ``ImpersonationSession`` row's id, so stopping can close the
    exact row that was opened instead of guessing at the latest one.

``django.contrib.auth.login`` cycles the session key on every call and
flushes the session, so the keys are written *after* logging in as the
customer and re-written after logging back in as the operator. That
cycling is also what makes the swap safe: a fixated session cookie does
not survive the switch.

Why the operator's id and not a full second session: the id is the
minimum that can restore the operator, and it is re-fetched from the
database on every request (``ImpersonationGuardMiddleware`` calls
:func:`end_if_operator_revoked`), so an operator who is deactivated or
demoted mid-takeover loses the customer's session on their next request
instead of carrying a snapshot of the privilege around.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar

from django.contrib.auth import get_user_model, login, logout
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy

from apps.accounts.models import ImpersonationSession

# The real operator's pk. Presence of this key means "this session is
# impersonating"; nothing else records that fact.
IMPERSONATOR_SESSION_KEY = "impersonator_user_id"

# The open ImpersonationSession row, so stop() closes the row start()
# opened rather than the most recent one for the pair.
RECORD_SESSION_KEY = "impersonation_record_id"

# Where the operator goes back to when the takeover ends, when it was
# started from a screen that asked for it (the live chat console).
RETURN_SESSION_KEY = "impersonation_return_path"

# Django's own backend path. login() needs a backend when the user did
# not arrive through authenticate(); ModelBackend is first in
# AUTHENTICATION_BACKENDS and is the one that would have answered for a
# password login.
_BACKEND = "django.contrib.auth.backends.ModelBackend"


# True while the request being served is a takeover. A context variable
# because the model that enforces the rule has no request to ask.
_publishing_blocked: ContextVar[bool] = ContextVar("impersonation_publishing_blocked", default=False)


class TakeoverRefusedError(PermissionDenied):
    """A takeover tried an action that only the customer may take.

    ``title`` and ``body`` are what ``ImpersonationGuardMiddleware`` shows
    the operator instead of an error page.
    """

    title = gettext_lazy("Not allowed during support access")
    body = gettext_lazy(
        "Support access can look at the account and adjust its settings, but cannot delete, pay, "
        "change who has access or send messages in the customer's name."
    )


class PublishingBlockedError(TakeoverRefusedError):
    """A takeover tried to schedule or publish a post."""

    title = gettext_lazy("Nothing was scheduled or published")
    body = gettext_lazy("Support access cannot schedule or publish in a customer's name.")


@contextmanager
def block_publishing():
    """Refuse scheduling for the duration of the block (one takeover request)."""
    token = _publishing_blocked.set(True)
    try:
        yield
    finally:
        _publishing_blocked.reset(token)


def publishing_blocked():
    """Whether the code running now is serving a takeover."""
    return _publishing_blocked.get()


def is_impersonating(request):
    """True when this session is an operator wearing a customer's account."""
    session = getattr(request, "session", None)
    return bool(session and session.get(IMPERSONATOR_SESSION_KEY))


def refuse_during_takeover(request):
    """Raise :class:`TakeoverRefusedError` when this request is a takeover.

    For a view that refuses only some of its actions (one ``action`` of a
    settings form); a view whose every change is refused uses
    :func:`no_takeover`.
    """
    if is_impersonating(request):
        raise TakeoverRefusedError("This action is not allowed during an impersonation session.")


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def no_takeover(view_func):
    """Refuse the view's changes during a takeover; reading it stays open.

    Only unsafe methods are refused, so a page that shows a form on GET and
    acts on POST can still be looked at while diagnosing.
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.method not in _SAFE_METHODS:
            refuse_during_takeover(request)
        return view_func(request, *args, **kwargs)

    return _wrapped


def real_user(request):
    """The human behind the request, ignoring any impersonation.

    Returns ``request.user`` normally. During a takeover it returns the
    operator, re-read from the database — so a demoted or deactivated
    operator stops being one on their very next request rather than at
    the end of a 14-day session.

    Returns None when there is no authenticated user at all, and falls
    back to ``request.user`` if the stored operator id no longer resolves
    to an active operator, which attaches no privilege to the session.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if not is_impersonating(request):
        return user

    return _active_operator(request.session[IMPERSONATOR_SESSION_KEY]) or user


def _active_operator(operator_id):
    """The account behind a takeover, or None once it stopped being an active operator."""
    return get_user_model().objects.filter(pk=operator_id, is_active=True, is_staff=True).first()


def end_if_operator_revoked(request):
    """End a takeover whose operator is no longer an active staff account.

    A takeover is delegated operator authority, so it lasts only while
    that authority does. Closes the audit row and logs the session out;
    the request then continues as an anonymous one. Returns True when it
    ended the session.
    """
    if not is_impersonating(request):
        return False
    if _active_operator(request.session[IMPERSONATOR_SESSION_KEY]) is not None:
        return False
    _close_record(request.session.get(RECORD_SESSION_KEY))
    logout(request)
    return True


def _close_record(record_id):
    if record_id:
        ImpersonationSession.objects.filter(pk=record_id, ended_at__isnull=True).update(ended_at=timezone.now())


def is_operator(request):
    """True when this request may use operator-only surfaces.

    Deliberately False while impersonating, even though the real user is
    staff: see the module docstring. This is the single predicate the
    staff console asks.
    """
    if is_impersonating(request):
        return False
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def is_superadmin(request):
    """True when this request may use the superadmin-only surfaces.

    The team has two levels, told apart by the flags Django already has:
    ``is_staff`` alone is support, ``is_staff`` with ``is_superuser`` is
    superadmin. Money, platform switches and the team itself are
    superadmin-only.
    """
    return is_operator(request) and request.user.is_superuser


def may_impersonate(operator, target):
    """Whether ``operator`` is allowed to take over ``target``.

    The two refusals that matter, in one place so a second entry point
    cannot implement only one of them:

    - the operator must be an active staff account;
    - the target must not be staff, and must not be the operator.
    """
    if operator is None or not operator.is_authenticated:
        return False
    if not (operator.is_active and operator.is_staff):
        return False
    if target is None or not target.is_active:
        return False
    if target.pk == operator.pk:
        return False
    # No lateral takeover between operators.
    return not target.is_staff


def start(request, target):
    """Log the session in as ``target``, remembering the operator.

    Raises ``PermissionDenied`` when :func:`may_impersonate` refuses, so
    a caller that forgets to check cannot accidentally allow a takeover.
    Returns the ``ImpersonationSession`` row it opened.
    """
    operator = real_user(request)
    if not may_impersonate(operator, target):
        raise PermissionDenied("You may not impersonate this account.")

    record = ImpersonationSession.objects.create(
        impersonator=operator,
        impersonated=target,
        impersonator_email=operator.email,
        impersonated_email=target.email,
        ip_address=client_ip(request),
    )

    # login() flushes and cycles the session, so the two keys have to be
    # written after it — not before, or they would be thrown away.
    login(request, target, backend=_BACKEND)
    request.session[IMPERSONATOR_SESSION_KEY] = str(operator.pk)
    request.session[RECORD_SESSION_KEY] = str(record.pk)
    return record


def _local_path(request, value: str) -> str | None:
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if not url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=False):
        return None
    return value


def product_path(request, value: str) -> str | None:
    """``value`` when it is a page of the product the takeover may open, else None.

    A path on this site only (no scheme, host or ``//``), never the
    administrative area: the operator is inside the customer's account,
    where ``/staff/`` is closed anyway.
    """
    from apps.accounts.operators import STAFF_AREA_PREFIX

    path = _local_path(request, value)
    if path is None or path.startswith(f"/{STAFF_AREA_PREFIX}"):
        return None
    return path


def staff_path(request, value: str) -> str | None:
    """``value`` when it is a screen of the administrative area to come back to, else None."""
    from apps.accounts.operators import STAFF_AREA_PREFIX

    path = _local_path(request, value)
    if path is None or not path.startswith(f"/{STAFF_AREA_PREFIX}"):
        return None
    return path


def stop(request):
    """Hand the session back to the operator and close the audit row.

    No re-authentication: the operator never stopped being themselves,
    the session only wore another name. Returns the operator, or None if
    this session was not impersonating (making a double-submitted "stop"
    harmless rather than a crash).
    """
    if not is_impersonating(request):
        return None

    operator_id = request.session.get(IMPERSONATOR_SESSION_KEY)
    record_id = request.session.get(RECORD_SESSION_KEY)

    # Close the trail before the session swap. If the login below fails
    # or the response is lost, an audit row saying the takeover ended is
    # the safe side of the error: the alternative is a row that stays
    # open forever and makes every later report ambiguous.
    _close_record(record_id)

    operator = _active_operator(operator_id)
    if operator is None:
        # The operator's account is gone, disabled or demoted while they
        # were inside. Restoring it is not an option, and leaving the
        # session as the customer would be worse, so end the session
        # entirely.
        logout(request)
        return None

    login(request, operator, backend=_BACKEND)
    # login() cycled the session, which already dropped both keys. Popped
    # anyway so this stays correct if that ever changes.
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    request.session.pop(RECORD_SESSION_KEY, None)
    return operator


def client_ip(request):
    """The visitor's IP, or None when it cannot be told.

    The one place the app derives it (``AccountAdapter.get_client_ip``), so
    records, the payment gateway's antifraud and the rate limits agree on who
    the client is.
    """
    from allauth.account.adapter import get_adapter

    try:
        return get_adapter(request).get_client_ip(request)
    except PermissionDenied:
        return None
