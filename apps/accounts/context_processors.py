"""Request context the chrome in ``base.html`` needs about who you are.

One processor. It adds ``impersonation`` during a takeover and
``is_superadmin`` for a superadmin of the team, which the administrative
navigation and screens read to leave out what support cannot use (the
views refuse it either way). The banner the first one feeds is not decoration: an operator who forgets they are
inside a customer's account and publishes a post, deletes an asset or
replies to a DM does it *as that customer*, and nothing in the rest of
the interface would look any different. So the flag is computed on every
authenticated request rather than in the one or two views that start and
stop the takeover.

Cost in the common case is one session-dict lookup, which is already in
memory — no query. Only an actual takeover pays for the operator row,
and :func:`apps.accounts.impersonation.real_user` is the single place
that reads it.
"""

from __future__ import annotations

from apps.accounts import impersonation


def impersonation_banner(request):
    """Expose ``impersonation`` while this session wears another account.

    Absent otherwise, so the partial costs one ``{% if %}`` on every
    normal page. Outside a takeover, ``is_superadmin`` for a superadmin.
    """
    if not impersonation.is_impersonating(request):
        return {"is_superadmin": True} if impersonation.is_superadmin(request) else {}

    operator = impersonation.real_user(request)
    return {
        "impersonation": {
            "operator": operator,
            "impersonated": request.user,
        }
    }
