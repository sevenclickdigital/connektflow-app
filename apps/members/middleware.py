"""RBAC middleware that resolves org role + workspace role on every request."""

from django.core.exceptions import PermissionDenied

from .models import OrgMembership, WorkspaceMembership

# Prefixes served straight off disk by a URLconf route rather than by a view.
# Nothing downstream of them reads request.org / request.workspace, so
# resolving that context would only cost a session read, a user query and two
# membership queries — once per file, and a media-heavy page requests dozens.
CONTEXT_FREE_PATH_PREFIXES = ("/static/", "/media/")


class RBACMiddleware:
    """Attach org and workspace context to the request.

    Sets:
        request.org - the Organization in use (or None)
        request.org_membership - the user's OrgMembership in it (or None)
        request.org_memberships - every OrgMembership of the user
        request.workspace - the current Workspace (or None, set by views)
        request.workspace_membership - the WorkspaceMembership (or None)

    A user may belong to several organizations. The one in use is the
    organization of the workspace they are in (from the URL, else the last
    one they opened); without such a workspace, or when they are only a
    guest of its organization, it is the organization they joined first.

    Resolution happens in process_view (not __call__) because
    request.resolver_match is only available after URL resolution,
    which occurs inside get_response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Initialize attributes; actual resolution happens in process_view
        request.org = None
        request.org_membership = None
        request.org_memberships = []
        request.workspace = None
        request.workspace_membership = None

        if request.path.startswith(CONTEXT_FREE_PATH_PREFIXES):
            return self.get_response(request)

        # Resolve org and workspace context early.
        # Workspace-from-URL resolution happens in process_view() (after URL
        # resolution). Here we only resolve org directly and fall back to
        # last_workspace_id for global pages that have no workspace_id in the URL.
        if hasattr(request, "user") and request.user.is_authenticated:
            request.org_memberships = list(
                OrgMembership.objects.filter(user=request.user).select_related("organization").order_by("invited_at")
            )

            # Fall back to last_workspace_id so global pages
            # (e.g. /notifications/) can still show workspace nav.
            # For workspace-specific URLs, process_view() will override this.
            if getattr(request.user, "last_workspace_id", None):
                ws_membership = (
                    WorkspaceMembership.objects.filter(
                        user=request.user,
                        workspace_id=request.user.last_workspace_id,
                        workspace__is_archived=False,
                    )
                    .select_related("workspace__organization", "custom_role")
                    .first()
                )
                if ws_membership:
                    request.workspace = ws_membership.workspace
                    request.workspace_membership = ws_membership

            # The organization of that workspace when the user belongs to
            # it, otherwise the first they joined.
            current_org_id = request.workspace.organization_id if request.workspace else None
            org_membership = next(
                (m for m in request.org_memberships if m.organization_id == current_org_id),
                request.org_memberships[0] if request.org_memberships else None,
            )
            if org_membership:
                request.org = org_membership.organization
                request.org_membership = org_membership

        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Resolve workspace context from URL kwargs.

        This runs after URL resolution, so resolver_match and view_kwargs
        are available.
        """
        # workspace_id first: it is a dict lookup, while ``request.user`` is
        # lazy and forces a session read plus a user query on every URL that
        # has no workspace in it at all.
        workspace_id = view_kwargs.get("workspace_id")
        if not workspace_id:
            return None

        if not hasattr(request, "user") or not request.user.is_authenticated:
            return None

        # Clear stale context from __call__ before resolving
        request.workspace = None
        request.workspace_membership = None

        ws_membership = (
            WorkspaceMembership.objects.filter(
                user=request.user,
                workspace_id=workspace_id,
            )
            .select_related("workspace__organization", "custom_role")
            .first()
        )
        if not ws_membership:
            raise PermissionDenied("You do not have access to this workspace.")

        request.workspace = ws_membership.workspace
        request.workspace_membership = ws_membership

        # Keep last_workspace_id in sync so global pages show the right workspace
        if request.user.last_workspace_id != ws_membership.workspace_id:
            request.user.last_workspace_id = ws_membership.workspace_id
            request.user.save(update_fields=["last_workspace_id"])

        # The organization in use follows the workspace in the URL.
        org_id = ws_membership.workspace.organization_id
        org_membership = next((m for m in request.org_memberships if m.organization_id == org_id), None)
        if org_membership:
            request.org = org_membership.organization
            request.org_membership = org_membership

        return None
