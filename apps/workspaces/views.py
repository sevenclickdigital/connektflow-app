import json
import zoneinfo
from urllib.parse import urlencode
from zoneinfo import available_timezones

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.impersonation import no_takeover, refuse_during_takeover
from apps.analytics.metrics import PLATFORM_PRIMARY
from apps.common.htmx import toast_response
from apps.common.periods import parse_period
from apps.media_library.services import schedule_stored_file_removal, tenant_stored_files
from apps.members.decorators import require_workspace_role
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.workspaces.services import NEW_BRAND_SESSION_KEY, create_brand

from . import dashboard
from .models import Workspace

#: Brazil's timezones, offered first in a brand's settings and named the way
#: people there call them (city names, the same in every language).
#: Brasília time comes first: it is the default.
BRAZIL_TIMEZONES = (
    ("America/Sao_Paulo", "Brasília (UTC-3)"),
    ("America/Manaus", "Manaus (UTC-4)"),
    ("America/Cuiaba", "Cuiabá (UTC-4)"),
    ("America/Campo_Grande", "Campo Grande (UTC-4)"),
    ("America/Porto_Velho", "Porto Velho (UTC-4)"),
    ("America/Boa_Vista", "Boa Vista (UTC-4)"),
    ("America/Rio_Branco", "Rio Branco (UTC-5)"),
    ("America/Noronha", "Fernando de Noronha (UTC-2)"),
)


@login_required
def workspace_list(request):
    memberships = WorkspaceMembership.objects.filter(user=request.user).select_related("workspace")
    workspaces = [m.workspace for m in memberships if not m.workspace.is_archived]
    return render(request, "workspaces/list.html", {"workspaces": workspaces})


@login_required
@require_POST
def workspace_create(request):
    """Create a new workspace in one of the user's organizations.

    ``organization`` in the form names which one (the brand switcher lists
    the brands by organization and creates inside each); without it, the
    organization in use. Either way the user has to be its owner or admin.
    """
    name = request.POST.get("name", "").strip()
    if not name:
        return redirect("dashboard")

    wanted = request.POST.get("organization") or (str(request.org.id) if request.org else "")
    membership = next((m for m in request.org_memberships if str(m.organization_id) == wanted), None)
    if membership is None or membership.org_role not in (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN):
        raise PermissionDenied("Insufficient organization role.")

    workspace = create_brand(membership.organization, name, request.user)

    # Set as current workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])
    # Until it is contracted, its checkout offers to give up on it (billing.views.checkout_cancel).
    request.session[NEW_BRAND_SESSION_KEY] = str(workspace.id)

    # A new workspace is the billable unit, so the next screen is the
    # contracting page rather than the calendar.
    return redirect("billing:subscribe_modal", workspace_id=workspace.id)


@login_required
@require_http_methods(["GET", "POST"])
def workspace_settings(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    is_owner_or_manager = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    if request.method == "POST":
        if not is_owner_or_manager:
            raise Http404

        action = request.POST.get("action")
        if action in ("archive_workspace", "delete_workspace"):
            refuse_during_takeover(request)

        if action == "archive_workspace" and is_owner_or_manager:
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1:
                    messages.error(request, _("Cannot archive the last workspace in the organization."))
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace.is_archived = True
                workspace.save(update_fields=["is_archived"])
            messages.success(request, _('Workspace "%(name)s" has been archived.') % {"name": workspace.name})
            return redirect("organizations:workspaces")

        if action == "unarchive_workspace" and is_owner_or_manager:
            workspace.is_archived = False
            workspace.save(update_fields=["is_archived"])
            messages.success(request, _('Workspace "%(name)s" has been restored.') % {"name": workspace.name})
            return redirect("workspaces:settings", workspace_id=workspace.id)

        if action == "delete_workspace" and is_owner_or_manager:
            with transaction.atomic():
                active_count = (
                    Workspace.objects.select_for_update()
                    .filter(organization=workspace.organization, is_archived=False)
                    .count()
                )
                if active_count <= 1 and not workspace.is_archived:
                    messages.error(request, _("Cannot delete the last workspace in the organization."))
                    return redirect("workspaces:settings", workspace_id=workspace.id)
                workspace_name = workspace.name
                schedule_stored_file_removal(tenant_stored_files([workspace.id]))
                workspace.delete()
            messages.success(
                request, _('Workspace "%(name)s" has been permanently deleted.') % {"name": workspace_name}
            )
            return redirect("organizations:workspaces")

        if action == "update_general":
            # The name, the timezone and the segments are one form with one
            # save: either all of it is stored or none.
            from apps.calendar.services import set_date_segments

            name = request.POST.get("name", "").strip()
            tz = request.POST.get("timezone", "").strip()
            if tz and tz not in available_timezones():
                messages.error(request, _("Invalid timezone."))
                return redirect("workspaces:settings", workspace_id=workspace.id)
            try:
                with transaction.atomic():
                    set_date_segments(workspace, request.POST.getlist("segments"))
                    if name:
                        workspace.name = name
                    if tz:
                        workspace.timezone = tz
                    workspace.save(update_fields=["name", "timezone", "updated_at"])
            except (ValidationError, ValueError):
                messages.error(request, _("Invalid segment."))
            else:
                messages.success(request, _("Workspace settings updated."))
            return redirect("workspaces:settings", workspace_id=workspace.id)

        name = request.POST.get("name", "").strip()

        if name:
            workspace.name = name

        # Handle logo deletion
        if request.POST.get("delete_icon") == "1":
            if workspace.icon:
                workspace.icon.delete(save=False)
        # Handle logo upload
        elif "icon" in request.FILES:
            icon = request.FILES["icon"]

            # The type comes from the file's bytes and the stored name gets
            # the matching extension: workspaces/icons/ is served publicly, by suffix.
            from apps.media_library.validators import image_storage_filename

            storage_name = image_storage_filename(icon)
            if not storage_name:
                messages.error(request, _("Logo must be a JPEG, PNG, WebP, or GIF image."))
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Validate file size (2 MB max)
            max_size = 2 * 1024 * 1024
            if icon.size > max_size:
                messages.error(request, _("Logo must be under 2 MB."))
                return redirect("workspaces:settings", workspace_id=workspace.id)

            # Delete old icon before saving new one
            if workspace.icon:
                workspace.icon.delete(save=False)
            icon.name = storage_name
            workspace.icon = icon

        workspace.save()
        messages.success(request, _("Workspace settings updated."))
        return redirect("workspaces:settings", workspace_id=workspace.id)

    active_count = Workspace.objects.filter(organization=workspace.organization, is_archived=False).count()
    is_last_active = active_count <= 1 and not workspace.is_archived
    can_archive = is_owner_or_manager and not workspace.is_archived and not is_last_active
    can_delete = is_owner_or_manager and not is_last_active

    from apps.calendar.services import date_segments_for

    return render(
        request,
        "workspaces/settings.html",
        {
            "workspace": workspace,
            "settings_active": "general",
            "is_owner_or_manager": is_owner_or_manager,
            "can_archive": can_archive,
            "can_delete": can_delete,
            "brazil_timezones": BRAZIL_TIMEZONES,
            "timezone_is_brazilian": workspace.effective_timezone in dict(BRAZIL_TIMEZONES),
            "all_timezones": sorted(available_timezones()),
            "date_segments": date_segments_for(workspace),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def approvals_settings(request, workspace_id):
    """The brand's approval flow: everyone in the brand sees it, owners and managers change it."""
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if not membership:
        raise Http404

    is_owner_or_manager = membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )

    if request.method == "POST":
        if not is_owner_or_manager:
            raise Http404

        mode = request.POST.get("approval_workflow_mode", "")
        if mode not in Workspace.ApprovalWorkflowMode.values:
            messages.error(request, _("Invalid approval workflow mode."))
        else:
            workspace.approval_workflow_mode = mode
            workspace.save(update_fields=["approval_workflow_mode", "updated_at"])
            messages.success(request, _("Approval workflow updated."))
        return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

    return render(
        request,
        "workspaces/approvals.html",
        {
            "workspace": workspace,
            "settings_active": "approvals",
            "is_owner_or_manager": is_owner_or_manager,
            "approval_modes": Workspace.ApprovalWorkflowMode,
        },
    )


def _pending_brand_invitations(workspace):
    """Invitations of the brand's organization that give access to this brand and still wait for an answer."""
    from apps.members.models import Invitation

    return Invitation.objects.for_org(workspace.organization_id).filter(
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
        workspace_assignments__contains=[{"workspace_id": str(workspace.id)}],
    )


@login_required
@no_takeover
@require_http_methods(["GET", "POST"])
def workspace_team(request, workspace_id):
    """Who is in this brand and with which role.

    Managed by the brand's owners and managers, or the owners/admins of the
    organization that owns it. Everyone else gets a 404, as on the other
    brand settings pages, so the page doesn't confirm the brand exists.
    """
    from apps.members import services as member_services

    workspace = Workspace.objects.select_related("organization").filter(id=workspace_id).first()
    if workspace is None or not member_services.can_manage_brand_team(request.user, workspace):
        raise Http404
    org = workspace.organization

    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "invite":
                role = request.POST.get("role", "")
                if role not in dict(member_services.assignable_brand_roles(request.user, workspace)):
                    raise ValueError(_("You cannot grant a role higher than your own in this brand."))
                member_services.create_invitation(
                    org=org,
                    email=request.POST.get("email", ""),
                    name=request.POST.get("name", ""),
                    org_role=OrgMembership.OrgRole.MEMBER,
                    workspace_assignments=[{"workspace_id": str(workspace.id), "role": role}],
                    invited_by=request.user,
                    inviter=request.user,
                )
                messages.success(request, _("Invitation sent."))
            elif action in ("resend_invite", "revoke_invite"):
                try:
                    invitation = (
                        _pending_brand_invitations(workspace).filter(id=request.POST.get("invitation_id")).first()
                    )
                except (ValueError, ValidationError):
                    # An id that is not a UUID: answered like one that is not pending.
                    invitation = None
                if invitation is None:
                    raise ValueError(_("This invitation is no longer pending."))
                if action == "resend_invite":
                    member_services.resend_invitation(invitation)
                    messages.success(request, _("Invitation sent."))
                else:
                    member_services.revoke_invitation(invitation)
                    messages.success(request, _("Invitation canceled."))
            else:
                # The person is looked up inside the brand's organization only,
                # so an id from another organization can't be acted on.
                member = OrgMembership.objects.filter(organization=org, user_id=request.POST.get("user_id")).first()
                if member is None:
                    raise ValueError(_("This person is not a member of the organization that owns this brand."))
                if action == "remove":
                    member_services.set_brand_role(workspace, member.user, None, by=request.user)
                    messages.success(request, _("Removed from the brand."))
                elif action in ("add", "change_role"):
                    member_services.set_brand_role(workspace, member.user, request.POST.get("role"), by=request.user)
                    messages.success(request, _("Team updated."))
                else:
                    raise ValueError(_("Unknown action."))
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("workspaces:team", workspace_id=workspace.id)

    memberships = list(
        WorkspaceMembership.objects.filter(workspace=workspace)
        .select_related("user")
        .order_by("user__name", "user__email")
    )
    in_brand = {m.user_id for m in memberships}
    assignable = member_services.assignable_brand_roles(request.user, workspace)
    addable = [
        m.user
        for m in OrgMembership.objects.filter(organization=org)
        .select_related("user")
        .order_by("user__name", "user__email")
        if m.user_id not in in_brand
    ]
    role_labels = dict(WorkspaceMembership.WorkspaceRole.choices)
    pending = [
        {
            "invitation": invitation,
            "role": next(
                role_labels.get(a.get("role"), "")
                for a in invitation.workspace_assignments
                if a["workspace_id"] == str(workspace.id)
            ),
        }
        for invitation in _pending_brand_invitations(workspace).select_related("invited_by").order_by("-created_at")
    ]
    return render(
        request,
        "workspaces/team.html",
        {
            "workspace": workspace,
            "memberships": memberships,
            "addable_members": addable,
            "pending_invitations": pending,
            "role_choices": assignable,
            "assignable_values": [value for value, _label in assignable],
            "all_roles": WorkspaceMembership.WorkspaceRole.choices,
            "settings_active": "team",
        },
    )


DASHBOARD_RANGES = (7, 30, 90)
DASHBOARD_DEFAULT_RANGE = 30
MONTHLY_GOAL_MAX = 999


def _workspace_tz(workspace):
    try:
        return zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _date_links(entries, workspace_id, can_plan):
    """Give each upcoming date its "See more" link (``more_url``), or none.

    Every date opens the special-dates page, which needs ``create_posts``. A
    special date opens there picked (``?date=<key>&day=<iso>``); the brand's own
    events are not in that page's list, so theirs opens it as it is.
    """
    dates_url = reverse("composer:create_dates", kwargs={"workspace_id": workspace_id})
    for entry in entries:
        if not can_plan:
            entry["more_url"] = ""
        elif entry.get("key") and not entry.get("event"):
            entry["more_url"] = f"{dates_url}?{urlencode({'date': entry['key'], 'day': entry['date'].isoformat()})}"
        else:
            entry["more_url"] = dates_url
    return entries


@login_required
@require_workspace_role("viewer")
@require_GET
def workspace_dashboard(request, workspace_id):
    """The brand's overview: what needs attention, results per network, the
    month's post goal and what has been working.

    ``?range=7|30|90`` or a custom ``?start=&end=`` sets the period of every number, ``?account=<id>`` the
    network the results chart, top posts and "what works" read from. Both are
    plain links; the chart's metric chips swap only the chart
    (``?partial=chart``), the goal card re-fetches itself
    (``?partial=goal``) after it is saved, and the "By network" cards load
    after the page (``?partial=networks``), each recomputing its account.
    """
    from apps.analytics import services as analytics
    from apps.social_accounts.models import AnalyticsPlatformConfig

    workspace = request.workspace
    perms = request.workspace_membership.effective_permissions
    tz = _workspace_tz(workspace)
    today = timezone.localdate(timezone=tz)

    is_team = request.workspace_membership.workspace_role != WorkspaceMembership.WorkspaceRole.CLIENT
    goal = dashboard.month_goal_progress(workspace, today, tz)
    context = {
        "workspace": workspace,
        "goal": goal,
        "can_set_goal": _can_set_goal(request.workspace_membership),
    }
    if request.htmx and request.GET.get("partial") == "goal":
        return render(request, "workspaces/partials/_dashboard_goal.html", context)

    period = parse_period(request.GET, choices=DASHBOARD_RANGES, default=DASHBOARD_DEFAULT_RANGE, today=today)
    days, end = period.days, period.end_param

    enabled = AnalyticsPlatformConfig.enabled_platforms() if perms.get("view_analytics") else []
    accounts = analytics.analytics_accounts(workspace, enabled) if enabled else []
    rated = [a for a in accounts if a.analytics_verdict is None]
    # The results pills name the network only ("Instagram"); the account name
    # joins it only when the brand has two accounts on the same network.
    platforms = [a.platform for a in rated]
    for account in rated:
        network = analytics.platform_name(account)
        account.pill_label = (
            network if platforms.count(account.platform) == 1 else f"{network} · {account.account_name}"
        )
    selected = next((a for a in rated if str(a.id) == request.GET.get("account")), None)
    if selected is None and rated:
        selected = analytics.preferred_account(rated)

    context.update(
        {
            "days": days,
            "period": period,
            "range_choices": DASHBOARD_RANGES,
            "accounts": accounts,
            "rated_accounts": rated,
            "active_account": selected,
            "primary_color": "var(--primary)",
            "reconnect_accounts": (
                dashboard.reconnect_accounts(workspace) if perms.get("manage_social_accounts") else []
            ),
            "failed_posts": dashboard.failed_post_count(workspace) if perms.get("create_posts") else 0,
            "can_view_analytics": bool(perms.get("view_analytics")),
            "can_connect_accounts": bool(perms.get("manage_social_accounts")),
            # The task board is the team's: clients work through the portal (tasks.views.require_board_access).
            "my_tasks": dashboard.my_open_tasks(workspace, request.user, today) if is_team else None,
            "day": dashboard.day_summary(workspace, request.user, perms, today, tz, is_team=is_team),
            "upcoming_dates": _date_links(
                dashboard.upcoming_dates_with_posts(workspace, today, tz), workspace.id, perms.get("create_posts")
            ),
            "today": today,
        }
    )
    if request.htmx and request.GET.get("partial") == "networks":
        # Each card recomputes its account's analytics: loaded after the page.
        context["network_cards"] = [dashboard.network_card(account, days, end) for account in accounts]
        return render(request, "workspaces/partials/_dashboard_networks.html", context)
    if selected is not None and request.htmx and request.GET.get("partial") == "chart":
        # The results chart computes the account's analytics: the page draws
        # its card at once and the chart loads into it (and again per metric chip).
        bundle = analytics.account_analytics_bundle(selected, days, end)
        chart = analytics.hero_chart_data(
            selected, days, metric=request.GET.get("chart_metric"), bundle=bundle, end=end
        )
        context.update(
            {
                # The names analytics/_hero_chart.html reads; chart_query keeps
                # the chosen network on its metric chips.
                "chart": chart,
                "chart_series_json": json.dumps([None if v is None else round(v, 4) for v in chart["derived"].series]),
                "chart_labels_json": json.dumps(chart["labels"]),
                "content_noun": analytics.content_noun(selected),
                "chart_query": urlencode({"account": selected.id}) + "&",
            }
        )
        return render(request, "analytics/_hero_chart.html", context)
    if selected is not None:
        primary = PLATFORM_PRIMARY.get(selected.platform, "")
        context.update(
            {
                "chart_query": urlencode({"account": selected.id}) + "&",
                "top_posts": analytics.all_posts_for(selected, days_filter=days, end=end, sort_key=None, page_size=4)[
                    "rows"
                ],
                "primary_metric": primary,
                "primary_label": analytics.metric_label(primary)
                if primary in analytics.hero_chart_metrics(selected)
                else "",
                "performance": analytics.post_performance(selected, days, tz, end),
            }
        )
    return render(request, "workspaces/dashboard.html", context)


def _can_set_goal(membership) -> bool:
    """Owners and managers plan the brand's month; the goal is theirs to set."""
    return membership is not None and membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    )


@login_required
@require_workspace_role("manager")
@require_POST
def dashboard_goal(request, workspace_id):
    """Set (or clear, with an empty value) the brand's monthly post goal."""
    workspace = request.workspace
    raw = (request.POST.get("monthly_post_goal") or "").strip()
    if raw == "":
        goal = None
    else:
        try:
            goal = int(raw)
        except ValueError:
            goal = 0
        if not 1 <= goal <= MONTHLY_GOAL_MAX:
            return toast_response(
                tone="error",
                title=_("Enter a goal between 1 and %(max)s posts.") % {"max": MONTHLY_GOAL_MAX},
            )
    workspace.monthly_post_goal = goal
    workspace.save(update_fields=["monthly_post_goal", "updated_at"])
    return toast_response(
        tone="success",
        title=_("Monthly goal saved.") if goal else _("Monthly goal removed."),
        events={"dashboardGoalChanged": True},
    )
