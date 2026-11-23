"""Gates for the administrative area, which no customer can enter.

``staff_required`` opens a screen to the whole team (support and
superadmin); ``superadmin_required`` to superadmins only. Both ask
``apps.accounts.impersonation``, so the area is closed while an operator
is inside a customer's account.

There is no organization or workspace in these URLs for a membership
check to key on — the screens list every account — so the decorators in
``apps/members/decorators.py`` cannot gate them.
"""

from __future__ import annotations

import functools

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from apps.accounts import impersonation


def _gate(predicate, message):
    """``login_required`` plus ``predicate``, answering 403 to a logged-in refusal.

    Not ``user_passes_test``: that redirects to the login page on failure,
    so an authenticated customer who found the URL would bounce between
    login and here forever.
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not predicate(request):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return login_required(_wrapped)

    return decorator


staff_required = _gate(impersonation.is_operator, "Staff access required.")
superadmin_required = _gate(impersonation.is_superadmin, "Superadmin access required.")
