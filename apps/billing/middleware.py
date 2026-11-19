"""Keeps an unpaid workspace on its contracting screen.

Off unless "Subscription required" is on (the plan screen, else
``BILLING_REQUIRE_SUBSCRIPTION``; ``services.subscription_required``): turning
it on locks every workspace that is neither paid nor comped, so it is flipped
only after the workspaces that predate billing have been dealt with.

Runs after ``RBACMiddleware``, which is what resolves ``request.workspace``
from the URL and refuses non-members. Only URLs that carry a
``workspace_id`` are gated: the account centre, the staff console and
sign-out stay reachable, so someone who will not pay can still switch
brand or leave.
"""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from apps.billing import services

#: URL namespaces a locked workspace may still open: its own billing
#: pages, which are where the lock sends people.
OPEN_NAMESPACES = frozenset({"billing"})


class SubscriptionGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if "workspace_id" not in view_kwargs:
            return None
        # None for an anonymous visitor: the view's own login check answers.
        workspace = getattr(request, "workspace", None)
        if workspace is None:
            return None
        # Read after the cheap checks: it is the plan screen's setting (cached), not a constant.
        if not services.subscription_required():
            return None
        if request.resolver_match.namespace in OPEN_NAMESPACES:
            return None
        if services.is_workspace_active(workspace):
            return None

        target = reverse("billing:subscribe_modal", kwargs={"workspace_id": workspace.id})
        if getattr(request, "htmx", False):
            # A plain redirect would be followed by the XHR and the whole
            # contracting page swapped into whatever fragment asked.
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)
