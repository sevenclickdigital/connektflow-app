"""Keeps an unpaid workspace on its contracting screen.

Off unless "Subscription required" is on (the plan screen, else
``BILLING_REQUIRE_SUBSCRIPTION``; ``services.subscription_required``): turning
it on locks every workspace that is neither paid nor comped, so it is flipped
only after the workspaces that predate billing have been dealt with.

Runs after ``RBACMiddleware``, which is what resolves ``request.workspace``
from the URL and refuses non-members. A URL that carries a ``workspace_id``
is gated on that brand. The account pages (``ACCOUNT_NAMESPACES``: team,
organizations, settings, notifications, API keys, shared media, brands)
are gated on the person: they open once any brand they belong to is in
use. The contracting and billing pages, the profile, sign-out, the "about
your business" step, the organizations screen (the brands are listed,
created and moved there; each brand's own lock answers when it is
opened), the staff console and the links that arrive from
outside (invitations, client portal, connection links, OAuth callbacks)
stay reachable, so someone who will not pay can still pay or leave.
"""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from apps.billing import services
from apps.workspaces.models import Workspace

#: URL namespaces a locked workspace may still open: its own billing
#: pages, which are where the lock sends people.
OPEN_NAMESPACES = frozenset({"billing"})

#: Pages that belong to no brand, closed while none of the person's brands is in use.
ACCOUNT_NAMESPACES = frozenset(
    {"organizations", "api_keys", "media_library_org", "members", "notifications", "settings_manager", "workspaces"}
)

#: Account pages that stay open all the same: the setup step before the
#: plan, the organizations and their brands (where a brand is created, moved
#: or given up on; opening one is what its own lock answers), an invitation
#: being accepted, an e-mail unsubscribe link, and the unread badge every
#: page polls.
OPEN_ACCOUNT_VIEWS = frozenset(
    {
        ("organizations", "about"),
        ("organizations", "welcome"),
        ("organizations", "workspaces"),
        ("organizations", "rename"),
        ("organizations", "create"),
        ("organizations", "move_brand"),
        ("workspaces", "create"),
        ("members", "accept_invite"),
        ("notifications", "unsubscribe"),
        ("notifications", "unread_count"),
    }
)


class SubscriptionGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if "workspace_id" not in view_kwargs:
            return self._gate_account_page(request)
        # None for an anonymous visitor: the view's own login check answers.
        workspace = getattr(request, "workspace", None)
        if workspace is None:
            return None
        if request.resolver_match.namespace in OPEN_NAMESPACES:
            return None
        if not services.is_workspace_locked(workspace):
            return None

        return _to_contracting(request, workspace)

    def _gate_account_page(self, request):
        """Send someone with no brand in use from an account page to their brand's contracting page."""
        match = request.resolver_match
        if match is None or match.namespace not in ACCOUNT_NAMESPACES:
            return None
        if (match.namespace, match.url_name) in OPEN_ACCOUNT_VIEWS:
            return None
        user = request.user
        if not user.is_authenticated or user.is_staff:
            return None
        if not services.subscription_required() or services.has_workspace_in_use(user):
            return None
        workspace = getattr(request, "workspace", None) or (
            Workspace.objects.filter(memberships__user=user, is_archived=False).order_by("created_at").first()
        )
        if workspace is None:
            # Nothing to contract (an operator without brands): the page decides.
            return None
        return _to_contracting(request, workspace)


def _to_contracting(request, workspace):
    """Redirect to the brand's contracting page; HTMX gets HX-Redirect instead."""
    target = reverse("billing:subscribe_modal", kwargs={"workspace_id": workspace.id})
    if getattr(request, "htmx", False):
        # A plain redirect would be followed by the XHR and the whole
        # contracting page swapped into whatever fragment asked.
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)
