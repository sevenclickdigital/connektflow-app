import calendar as cal_mod
from collections import defaultdict
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone as django_tz
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.signals import create_organization_with_brand
from apps.accounts.signup_links import custom_signup_url, signup_allowed
from apps.billing import services as billing_services
from apps.calendar.services import day_label, month_label, week_label, weekday_names
from apps.common.validators import clean_display_name
from apps.composer.models import PlatformPost, Post, Tag
from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_http_methods(["GET", "POST"])
def settings_view(request):
    org = request.org
    is_owner = request.org_membership.org_role == OrgMembership.OrgRole.OWNER

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete_organization":
            return _handle_org_deletion(request, org)
        elif action == "delete_organization_now":
            return _handle_immediate_org_deletion(request, org)
        elif action == "cancel_deletion":
            _handle_cancel_deletion(request, org)
        return redirect("organizations:settings")

    context = {
        "organization": org,
        # Reached from the organizations page, so that is what stays lit in the menu.
        "settings_active": "workspaces",
        "is_owner": is_owner,
    }
    return render(request, "organizations/settings.html", context)


def needs_welcome(request, workspace):
    """Whether the user should name their organization and brand first.

    True for the owner of an organization that still has its sign-up name,
    looking at one of its brands. Asked by the contracting page, which
    shows the welcome step before the plan, and by the dashboard.
    """
    membership = request.org_membership
    return bool(
        membership
        and membership.org_role == OrgMembership.OrgRole.OWNER
        and membership.organization_id == workspace.organization_id
        and membership.organization.is_unnamed
    )


@login_required
@require_POST
def welcome_view(request):
    """Name the organization and its first brand, right after sign-up.

    The first thing a new account is asked: both were created with
    placeholder names, and the organization's goes on invitations and
    invoices. Then on to choosing between the free trial and the plan.
    """
    workspace = Workspace.objects.filter(
        id=request.POST.get("workspace") or None,
        memberships__user=request.user,
    ).first()
    if workspace is None or not needs_welcome(request, workspace):
        return redirect("dashboard")

    organization_name = clean_display_name(request.POST.get("organization_name", ""))
    brand_name = clean_display_name(request.POST.get("brand_name", ""))
    if not organization_name or not brand_name or organization_name == Organization.DEFAULT_NAME:
        messages.error(request, _("Enter the name of your organization and of your brand."))
        return redirect("billing:subscribe_modal", workspace_id=workspace.id)
    tax_id = billing_services.valid_tax_id(request.POST.get("tax_id"))
    phone = billing_services.valid_phone(request.POST.get("phone"))
    if not tax_id or not phone:
        messages.error(request, _("Enter a valid CPF or CNPJ and a phone with area code."))
        return redirect("billing:subscribe_modal", workspace_id=workspace.id)

    with transaction.atomic():
        organization = workspace.organization
        organization.name = organization_name
        organization.save(update_fields=["name"])
        workspace.name = brand_name
        workspace.save(update_fields=["name"])
        billing_services.set_payer_identity(organization, tax_id=tax_id, phone=phone)
    return redirect("billing:subscribe_modal", workspace_id=workspace.id)


#: How many organizations one person may own. Far above any real need (a
#: group of companies has a handful); it only stops a script from filling
#: the database with empty ones.
MAX_OWNED_ORGANIZATIONS = 20


@login_required
@require_POST
def create_view(request):
    """Create another organization for the user, with its first brand.

    For someone who runs separate businesses: each organization has its own
    brands, team, API keys and payer. Not offered to an account that owns
    nothing on purpose (an operator has no organization at all).
    """
    if not request.org_memberships:
        raise PermissionDenied("Only accounts with an organization can create another.")

    organization_name = clean_display_name(request.POST.get("organization_name", ""))
    brand_name = clean_display_name(request.POST.get("brand_name", ""))
    if not organization_name or not brand_name:
        messages.error(request, _("Enter the name of the organization and of its first brand."))
        return redirect("organizations:workspaces")
    tax_id = billing_services.valid_tax_id(request.POST.get("tax_id"))
    phone = billing_services.valid_phone(request.POST.get("phone"))
    if not tax_id or not phone:
        messages.error(request, _("Enter a valid CPF or CNPJ and a phone with area code."))
        return redirect("organizations:workspaces")

    owned = sum(1 for m in request.org_memberships if m.org_role == OrgMembership.OrgRole.OWNER)
    if owned >= MAX_OWNED_ORGANIZATIONS:
        messages.error(request, _("You have reached the number of organizations one account can own."))
        return redirect("organizations:workspaces")

    with transaction.atomic():
        workspace = create_organization_with_brand(
            request.user, organization_name=organization_name, brand_name=brand_name
        )
        # Same as a brand created in an existing organization: the row is
        # there for the quota and the staff console, and contracting is next.
        billing_services.ensure_subscription(workspace)
        billing_services.set_payer_identity(workspace.organization, tax_id=tax_id, phone=phone)
    return redirect("billing:subscribe_modal", workspace_id=workspace.id)


@login_required
@require_org_role(OrgMembership.OrgRole.MEMBER)
def workspaces_view(request):
    """Account centre → Organizations: each one with its brands underneath.

    Lists the organizations the user belongs to, newest first, then those
    where they were only added to a brand. Read only from the user's own
    memberships, so nothing they don't belong to can appear. The numbers of
    an organization (members, connected channels, owner) and the members of
    its brands are shown only to someone who belongs to it.
    """
    org = request.org
    org_roles = {m.organization_id: m.org_role for m in request.org_memberships}
    brand_roles = dict(
        WorkspaceMembership.objects.filter(user=request.user).values_list("workspace_id", "workspace_role")
    )
    managing_roles = (WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER)
    org_admin_roles = (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)

    def group_for(organization):
        org_role = org_roles.get(organization.id)
        return {
            "organization": organization,
            "is_current": organization.id == org.id,
            "is_member": org_role is not None,
            "can_edit": org_role in org_admin_roles,
            "brands": [],
        }

    memberships = sorted(request.org_memberships, key=lambda m: m.organization.created_at, reverse=True)
    groups = {m.organization_id: group_for(m.organization) for m in memberships}

    people = defaultdict(set)
    for organization_id, user_id, org_role, name, email in OrgMembership.objects.filter(
        organization_id__in=org_roles.keys()
    ).values_list("organization_id", "user_id", "org_role", "user__name", "user__email"):
        people[organization_id].add(user_id)
        if org_role == OrgMembership.OrgRole.OWNER:
            groups[organization_id].setdefault("owner_name", name or email.split("@")[0])
    channels = dict(
        SocialAccount.objects.filter(
            workspace__organization_id__in=org_roles.keys(),
            workspace__is_archived=False,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .values_list("workspace__organization_id")
        .annotate(total=Count("id"))
    )
    for organization_id, group in groups.items():
        group["member_count"] = len(people[organization_id])
        group["channel_count"] = channels.get(organization_id, 0)
        group["brand_count"] = 0

    workspaces = (
        Workspace.objects.filter(
            Q(organization_id__in=org_roles.keys()) | Q(id__in=brand_roles.keys(), is_archived=False)
        )
        .select_related("organization")
        .prefetch_related("memberships__user")
        .order_by("organization__name", "is_archived", "name")
    )
    for ws in workspaces:
        group = groups.get(ws.organization_id)
        if group is None:
            group = groups[ws.organization_id] = group_for(ws.organization)
        brand_role = brand_roles.get(ws.id)
        entry = {
            "workspace": ws,
            "is_member": brand_role is not None,
            "can_manage": brand_role in managing_roles or org_roles.get(ws.organization_id) in org_admin_roles,
        }
        if group["is_member"]:
            members = list(ws.memberships.all())
            entry.update(members=members, member_count=len(members))
        else:
            entry["role"] = brand_role
        group["brands"].append(entry)
        if group["is_member"] and not ws.is_archived:
            group["brand_count"] += 1

    org_groups = list(groups.values())
    # Position of each card under the "by name" order, applied in the page.
    by_name = sorted(org_groups, key=lambda g: (not g["is_member"], g["organization"].name.casefold()))
    for position, group in enumerate(by_name):
        group["name_position"] = position
    own = [g for g in org_groups if g["is_member"]]
    context = {
        "org_groups": org_groups,
        "totals": {
            "organizations": len(own),
            # Someone in two organizations is one person.
            "members": len(set().union(*people.values())) if people else 0,
            "brands": sum(g["brand_count"] for g in own),
            "channels": sum(g["channel_count"] for g in own),
        },
        "settings_active": "workspaces",
    }
    return render(request, "organizations/workspaces.html", context)


@login_required
@require_POST
def rename_view(request):
    """Rename one of the user's organizations, from the organizations page.

    The organization comes in the form, since the page lists several; only
    one the user owns or administers is accepted.
    """
    wanted = request.POST.get("organization", "")
    membership = next(
        (
            m
            for m in request.org_memberships
            if str(m.organization_id) == wanted
            and m.org_role in (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
        ),
        None,
    )
    if membership is None:
        raise PermissionDenied("Only an owner or admin of the organization can rename it.")
    _handle_name_update(request, membership.organization)
    return redirect("organizations:workspaces")


def _handle_name_update(request, org):
    """Handle organization name change.

    The name is interpolated into the Subject of every invitation this org
    sends ("You've been invited to join <name> on Connekt Flow"), so it is
    cleaned here rather than trusted: control characters out, whitespace
    collapsed, length bounded.
    """
    name = clean_display_name(request.POST.get("name", ""))
    if not name:
        messages.error(request, _("Organization name cannot be empty."))
        return

    org.name = name
    org.save(update_fields=["name"])
    messages.success(request, _("Organization name updated."))


@transaction.atomic
def _handle_org_deletion(request, org):
    """Schedule organization deletion for 14 days from now (owner only)."""
    if request.org_membership.org_role != OrgMembership.OrgRole.OWNER:
        raise PermissionDenied

    from apps.organizations.tasks import execute_scheduled_org_deletion
    from apps.social_accounts.youtube_compliance import purge_organization

    org = type(org).objects.select_for_update().get(pk=org.pk)

    grace = timedelta(days=14)
    org.deletion_requested_at = django_tz.now()
    org.deletion_scheduled_for = django_tz.now() + grace
    org.save(update_fields=["deletion_requested_at", "deletion_scheduled_for"])
    purge_organization(org)

    execute_scheduled_org_deletion(str(org.id), schedule=grace)

    messages.success(request, _("Organization scheduled for deletion in 14 days."))
    return redirect("organizations:settings")


def _handle_immediate_org_deletion(request, org):
    """Permanently delete an organization right now (owner only).

    When it was the owner's only organization their account goes with it
    (``Organization.hard_delete``): they are logged out and sent to sign-up
    to start fresh. An owner who keeps another organization stays signed
    in and lands on it.
    """
    if request.org_membership.org_role != OrgMembership.OrgRole.OWNER:
        raise PermissionDenied

    from background_task.models import Task

    Task.objects.filter(
        task_name="apps.organizations.tasks.execute_scheduled_org_deletion",
        task_params__contains=str(org.id),
    ).delete()

    org.hard_delete(requesting_user=request.user)
    if User.objects.filter(pk=request.user.pk).exists():
        # They keep another organization, so the account stays and so does
        # the session: back to what is left.
        messages.success(request, _("The organization was deleted."))
        return redirect("dashboard")
    logout(request)
    # With SIGNUP_MODE invite/closed and no custom URL, the signup page would only
    # say sign-up is closed, so send them to log in instead.
    return redirect(custom_signup_url(request) or ("account_signup" if signup_allowed(request) else "account_login"))


def _handle_cancel_deletion(request, org):
    """Cancel a pending organization deletion (owner only).

    Also removes the queued background-task row so the worker isn't holding
    a no-op scheduled for 14 days out.
    """
    if request.org_membership.org_role != OrgMembership.OrgRole.OWNER:
        raise PermissionDenied

    from background_task.models import Task

    Task.objects.filter(
        task_name="apps.organizations.tasks.execute_scheduled_org_deletion",
        task_params__contains=str(org.id),
    ).delete()

    org.deletion_requested_at = None
    org.deletion_scheduled_for = None
    org.save(update_fields=["deletion_requested_at", "deletion_scheduled_for"])
    messages.success(
        request,
        _("Organization deletion cancelled. Reconnect any YouTube channels removed when deletion was requested."),
    )


@login_required
def cross_workspace_calendar(request):
    """Org-level calendar showing all workspaces' posts, color-coded by workspace."""
    org = request.org
    if not org:
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Organization required.")

    # Get workspaces the user has membership in
    user_workspace_ids = set(
        WorkspaceMembership.objects.filter(user=request.user).values_list("workspace_id", flat=True)
    )
    workspaces = Workspace.objects.filter(
        organization=org,
        id__in=user_workspace_ids,
        is_archived=False,
    ).order_by("name")

    # Workspace filter
    selected_ws_ids = request.GET.getlist("workspace")
    filtered_workspaces = workspaces.filter(id__in=selected_ws_ids) if selected_ws_ids else workspaces

    # View type (month/week/day)
    view_type = request.GET.get("view", "month")
    if view_type not in {"month", "week", "day"}:
        view_type = "month"

    target_date_str = request.GET.get("date")
    if target_date_str:
        try:
            target_date = date.fromisoformat(target_date_str)
        except (ValueError, TypeError):
            target_date = date.today()
    else:
        target_date = date.today()

    # Social accounts across filtered workspaces (for channel filter)
    social_accounts = (
        SocialAccount.objects.publishable()
        .filter(
            workspace__in=filtered_workspaces,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .select_related("workspace")
        .order_by("platform", "account_name")
    )

    # Tags across filtered workspaces
    all_tags = sorted(set(Tag.objects.filter(workspace__in=filtered_workspaces).values_list("name", flat=True)))

    # Base PlatformPost queryset with filters - each chip is one PP.
    from django.db.models.functions import Coalesce

    base_pps = (
        PlatformPost.objects.filter(post__workspace__in=filtered_workspaces)
        .select_related("post__workspace", "post__author", "social_account")
        .prefetch_related("post__media_attachments__media_asset")
        .annotate(effective_at=Coalesce("scheduled_at", "post__scheduled_at"))
    )

    # Channel filter
    channel = request.GET.get("channel")
    if channel:
        base_pps = base_pps.filter(social_account_id=channel)

    # Tag filter
    tag = request.GET.get("tag")
    if tag:
        base_pps = base_pps.filter(post__tags__contains=[tag])

    # Status filter — editorial status now lives on the PlatformPost itself.
    status = request.GET.get("status")
    if status:
        base_pps = base_pps.filter(status=status)

    # Workspace colors for legend
    workspace_colors = {}
    for ws in workspaces:
        workspace_colors[str(ws.id)] = ws.primary_color or "#F97316"

    today = date.today()

    # Build view-specific data
    if view_type == "week":
        context = _build_week_context(base_pps, target_date, today)
    elif view_type == "day":
        context = _build_day_context(base_pps, target_date, today)
    else:
        context = _build_month_context(base_pps, target_date, today)

    context.update(
        {
            "organization": org,
            "workspaces": workspaces,
            "selected_workspace_ids": selected_ws_ids,
            "workspace_colors": workspace_colors,
            "target_date": target_date,
            "default_workspace": workspaces.first(),
            "day_names": weekday_names(),
            "settings_active": "calendars",
            "view_type": view_type,
            "social_accounts": social_accounts,
            "all_tags": all_tags,
            "status_choices": Post.Status.choices,
        }
    )
    return render(request, "organizations/cross_calendar.html", context)


def _build_month_context(base_pps, target_date, today):
    year, month = target_date.year, target_date.month
    cal = cal_mod.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    first_day = weeks[0][0]
    last_day = weeks[-1][6]

    platform_posts = base_pps.filter(
        effective_at__date__gte=first_day,
        effective_at__date__lte=last_day,
    ).order_by("effective_at")

    posts_by_date = defaultdict(list)
    for pp in platform_posts:
        if pp.effective_at:
            posts_by_date[pp.effective_at.date()].append(pp)

    calendar_weeks = []
    for week in weeks:
        week_data = []
        for day in week:
            day_posts = posts_by_date.get(day, [])
            week_data.append(
                {
                    "date": day,
                    "is_current_month": day.month == month,
                    "is_today": day == today,
                    "is_past": day < today,
                    "posts": day_posts[:5],
                    "total_posts": len(day_posts),
                    "overflow": max(0, len(day_posts) - 5),
                }
            )
        calendar_weeks.append(week_data)

    prev_month = (date(year, month, 1) - timedelta(days=1)).replace(day=1)
    next_month = (date(year, month, 28) + timedelta(days=4)).replace(day=1)

    return {
        "calendar_weeks": calendar_weeks,
        "period_label": month_label(date(year, month, 1)),
        "prev_date": prev_month.isoformat(),
        "next_date": next_month.isoformat(),
    }


def _build_week_context(base_pps, target_date, today):
    monday = target_date - timedelta(days=target_date.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]

    platform_posts = base_pps.filter(
        effective_at__date__gte=week_days[0],
        effective_at__date__lte=week_days[6],
    ).order_by("effective_at")

    posts_by_slot = defaultdict(list)
    for pp in platform_posts:
        if pp.effective_at:
            key = (pp.effective_at.date(), pp.effective_at.hour)
            posts_by_slot[key].append(pp)

    hours = list(range(0, 24))

    week_slots = []
    for hour in hours:
        day_slots = []
        for day in week_days:
            key = (day, hour)
            day_slots.append((day, posts_by_slot.get(key, [])))
        week_slots.append((hour, day_slots))

    from django.utils import timezone as _tz

    return {
        "week_days": week_days,
        "hours": hours,
        "week_slots": week_slots,
        "today": today,
        "current_hour": _tz.now().hour,
        "period_label": week_label(week_days[0], week_days[6]),
        "prev_date": (monday - timedelta(weeks=1)).isoformat(),
        "next_date": (monday + timedelta(weeks=1)).isoformat(),
    }


def _build_day_context(base_pps, target_date, today):
    platform_posts = base_pps.filter(effective_at__date=target_date).order_by("effective_at")

    posts_by_hour = defaultdict(list)
    for pp in platform_posts:
        if pp.effective_at:
            posts_by_hour[pp.effective_at.hour].append(pp)

    hours = list(range(0, 24))

    day_slots = [(hour, posts_by_hour.get(hour, [])) for hour in hours]

    now = django_tz.now()

    return {
        "day_slots": day_slots,
        "hours": hours,
        "today": today,
        "is_today": target_date == today,
        "is_past_day": target_date < today,
        "current_hour": now.hour,
        "period_label": day_label(target_date),
        "prev_date": (target_date - timedelta(days=1)).isoformat(),
        "next_date": (target_date + timedelta(days=1)).isoformat(),
    }
