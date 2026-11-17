"""Context processors for sidebar and global template data."""


def sidebar_context(request):
    """Inject sidebar data into every template context.

    Provides:
        sidebar_workspaces: list of workspace objects the user belongs to
        sidebar_org_groups: those workspaces by organization, for the switcher
        sidebar_unread_inbox_count: unread inbox messages, for the badge
    """
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

    analytics_enabled_platforms = AnalyticsPlatformConfig.enabled_platforms()

    # User's workspaces (non-archived)
    workspace_memberships = (
        WorkspaceMembership.objects.filter(
            user=request.user,
            workspace__is_archived=False,
        )
        .select_related("workspace__organization")
        .order_by("workspace__name")
    )
    sidebar_workspaces = [wm.workspace for wm in workspace_memberships]

    # The same brands under the organization each belongs to. The user's own
    # organizations come first, in the order they joined them, each with
    # whether they may create a brand in it; organizations they only visit
    # as a guest of a brand follow.
    groups = {}
    for membership in getattr(request, "org_memberships", []):
        groups[membership.organization_id] = {
            "organization": membership.organization,
            "workspaces": [],
            "can_create": membership.org_role in ("owner", "admin"),
        }
    for ws in sidebar_workspaces:
        group = groups.setdefault(
            ws.organization_id, {"organization": ws.organization, "workspaces": [], "can_create": False}
        )
        group["workspaces"].append(ws)
    # Whether the create-brand form is on the page at all: the user may
    # create one in at least one of their organizations.
    can_create_workspace = any(group["can_create"] for group in groups.values())
    # The switcher only switches, so an organization with no brand to open
    # is left out of it (it is still listed in the account centre).
    sidebar_org_groups = [g for g in groups.values() if g["workspaces"]]

    workspace = getattr(request, "workspace", None)

    # Unread inbox count for sidebar badge
    sidebar_unread_inbox_count = 0
    if workspace:
        from apps.inbox.services import unread_count

        sidebar_unread_inbox_count = unread_count(workspace.id)

    # Pending approval count for badge
    sidebar_pending_approvals = 0
    if workspace:
        from apps.approvals.services import pending_approval_count

        sidebar_pending_approvals = pending_approval_count(workspace.id)

    # Idea columns and tags for the quick-create modal in the sidebar
    sidebar_idea_columns = []
    sidebar_idea_tags = []
    if workspace:
        from apps.composer.models import IdeaGroup, Tag

        groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")
        sidebar_idea_columns = [{"id": str(g.id), "label": g.name} for g in groups] if groups.exists() else []
        sidebar_idea_tags = list(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    # Trustpilot review ask: only for users with at least one connected channel,
    # in any of their workspaces, so pages without a workspace (settings,
    # members) don't make it flicker in and out. Closing it lasts until the
    # next login.
    show_review_banner = False
    dismissed_at = request.user.review_banner_dismissed_at
    last_login = request.user.last_login
    if dismissed_at is None or (last_login is not None and last_login > dismissed_at):
        show_review_banner = SocialAccount.objects.filter(
            workspace_id__in=[ws.id for ws in sidebar_workspaces],
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).exists()

    return {
        "sidebar_workspaces": sidebar_workspaces,
        "sidebar_org_groups": sidebar_org_groups,
        "can_create_organization": bool(getattr(request, "org_memberships", [])),
        "can_create_workspace": can_create_workspace,
        "sidebar_unread_inbox_count": sidebar_unread_inbox_count,
        "sidebar_pending_approvals": sidebar_pending_approvals,
        "sidebar_idea_columns": sidebar_idea_columns,
        "sidebar_idea_tags": sidebar_idea_tags,
        "analytics_enabled_platforms": analytics_enabled_platforms,
        "show_review_banner": show_review_banner,
    }
