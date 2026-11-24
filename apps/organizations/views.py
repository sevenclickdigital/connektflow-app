import calendar as cal_mod
import logging
import uuid
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone as django_tz
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
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
from apps.organizations import emails
from apps.organizations.forms import AboutBusinessForm
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces import services as workspace_services
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request, organization_id=None):
    """Account centre → Organizations → Edit: one organization's page.

    Its name and billing data (saved by ``edit_view``) and, for the owner,
    the danger zone: delete it, or cancel its pending deletion. The
    organizations page names which one (``organization_id``); without it,
    the organization in use. Only an owner or admin of that organization
    reaches the page; anyone else gets a 404.
    """
    from apps.billing.views import payer_summary

    membership = next(
        (
            m
            for m in request.org_memberships
            if m.organization_id == (organization_id or (request.org.id if request.org else None))
            and m.org_role in (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
        ),
        None,
    )
    if membership is None:
        raise Http404
    org = membership.organization
    is_owner = membership.org_role == OrgMembership.OrgRole.OWNER
    here = redirect("organizations:manage", organization_id=org.id)

    if request.method == "POST":
        if not is_owner:
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "delete_organization":
            _handle_org_deletion(request, org)
        elif action == "delete_organization_now":
            return _handle_immediate_org_deletion(request, org)
        elif action == "cancel_deletion":
            _handle_cancel_deletion(request, org)
        return here

    brands = list(Workspace.objects.filter(organization=org).order_by("is_archived", "name"))
    for brand in brands:
        brand.in_use = not brand.is_archived and billing_services.is_workspace_active(brand)
    context = {
        "organization": org,
        # Reached from the organizations page, so that is what stays lit in the menu.
        "settings_active": "workspaces",
        "is_owner": is_owner,
        "payer": payer_summary(org),
        # What deleting takes away, named in the danger zone.
        "brands": brands,
        "brands_in_use": sum(brand.in_use for brand in brands),
    }
    return render(request, "organizations/settings.html", context)


def needs_welcome(request, workspace):
    """Whether the account is still being set up by its owner.

    True for the owner of the organization, looking at one of its brands,
    while the organization has its sign-up name, or once it has been named
    on the "about your business" step, until one of its brands is in use
    (paid, comped or on trial). The about step stays open meanwhile, and the
    checkout shows it as the first of the setup steps.

    An organization with a brand in use is set up, whatever its name and
    whether or not it answered the about step: another brand created in it
    goes straight to its plan.
    """
    membership = request.org_membership
    if not (
        membership
        and membership.org_role == OrgMembership.OrgRole.OWNER
        and membership.organization_id == workspace.organization_id
    ):
        return False
    organization = membership.organization
    if billing_services.organization_has_brand_in_use(organization):
        return False
    return organization.is_unnamed or organization.about_answered_at is not None


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


def needs_about(request, workspace):
    """Whether the new account's owner is still to be asked about their business.

    Asked once, before the plan; answering closes it.
    """
    return workspace.organization.about_answered_at is None and needs_welcome(request, workspace)


@login_required
@require_http_methods(["GET", "POST"])
def about_view(request):
    """Ask a new account what it does, its name, one question after it, its size and how it found us.

    Shown right after sign-up and before the plan, and not skippable (the
    checkout's first step links back to it while the account is being set
    up). The organization is named here, asked after the activity; the
    checkout asks one more question after it, and the answers show on the
    administrative Customers screen.
    """
    workspace = Workspace.objects.filter(
        id=request.GET.get("workspace") or request.POST.get("workspace") or None,
        memberships__user=request.user,
    ).first()
    # Open while the account is being set up, answered or not: the checkout
    # links back here to change the answers until a brand is in use.
    if workspace is None or not needs_welcome(request, workspace):
        return redirect("dashboard")

    organization = workspace.organization
    form = AboutBusinessForm(instance=organization)
    if request.method == "POST":
        form = AboutBusinessForm(request.POST, instance=organization)
        if not form.is_valid():
            return _render_about(request, form, workspace)
        form.save(commit=False)
        organization.about_answered_at = django_tz.now()
        organization.save(
            update_fields=[
                "name",
                "business_activity",
                "team_size",
                "referral_source",
                "referral_source_other",
                "client_count",
                "business_segment",
                "about_answered_at",
            ]
        )
        return redirect("billing:checkout", workspace_id=workspace.id)
    return _render_about(request, form, workspace)


#: What each activity card says under its name.
ACTIVITY_DESCRIPTIONS = {
    Organization.Activity.ENTREPRENEUR: gettext_lazy(
        "I have my own business, already running or getting off the ground."
    ),
    Organization.Activity.FREELANCER: gettext_lazy(
        "I work on projects on my own, without a company or agency between me and the client."
    ),
    Organization.Activity.AGENCY: gettext_lazy("I own or work at an agency that looks after other brands."),
    Organization.Activity.MARKETING_ANALYST: gettext_lazy(
        "I am on a company's marketing team and look after its social media."
    ),
}


def _render_about(request, form, workspace):
    # The name is asked after the activity: every wording goes to the page,
    # which switches it as a card is picked, starting from the one chosen.
    name_questions = {
        value: [str(text) for text in Organization.name_question(value)]
        for value in [*Organization.Activity.values, ""]
    }
    # The question after the activity, per activity: its field and its label.
    extra_questions = {
        value: [field, str(label)]
        for value in Organization.Activity.values
        if (question := Organization.extra_question(value))
        for field, label in [question]
    }
    activity = form["business_activity"].value()
    if activity not in Organization.Activity.values:
        activity = ""
    return render(
        request,
        "organizations/about.html",
        {
            "form": form,
            "workspace": workspace,
            "activities": [
                (value, label, ACTIVITY_DESCRIPTIONS[value]) for value, label in Organization.Activity.choices
            ],
            "team_sizes": Organization.TeamSize.choices,
            "sources": Organization.Source.choices,
            "name_question": dict(zip(("label", "placeholder"), name_questions[activity], strict=True)),
            "name_questions": {"activity": activity, "questions": name_questions, "extras": extra_questions},
            "extra_question": dict(zip(("field", "label"), extra_questions.get(activity, ["", ""]), strict=True)),
            "client_counts": Organization.ClientCount.choices,
            "segments": Organization.Segment.choices,
        },
    )


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
@require_POST
def move_brand_view(request):
    """Move a brand to another organization of the same person.

    Both the brand's organization and the destination are looked up among the
    organizations where the person is owner or admin, so an id from the form
    pointing anywhere else is a 404. The rules of the move itself, billing
    included, are in ``workspaces.services.move_brand``.
    """
    allowed = workspace_services.movable_organization_ids(request.user)
    try:
        brand_id = uuid.UUID(request.POST.get("brand", ""))
        destination_id = uuid.UUID(request.POST.get("destination", ""))
    except ValueError as error:
        raise Http404 from error
    workspace = get_object_or_404(Workspace, id=brand_id, organization_id__in=allowed)
    destination = get_object_or_404(Organization, id__in=allowed, pk=destination_id)

    try:
        workspace_services.move_brand(workspace, destination)
    except workspace_services.BrandMoveError as error:
        messages.error(request, str(error))
        return redirect("organizations:workspaces")
    logger.info("User %s moved brand %s to organization %s", request.user.pk, workspace.pk, destination.pk)
    messages.success(
        request,
        _("%(brand)s is now in %(organization)s.") % {"brand": workspace.name, "organization": destination.name},
    )
    return redirect("organizations:workspaces")


@login_required
@require_org_role(OrgMembership.OrgRole.MEMBER)
def workspaces_view(request):
    """Account centre → Organizations: each one with its brands underneath.

    Lists the organizations the user owns or administers, newest first,
    then the others they belong to, then those where they were only added
    to a brand. Read only from the user's own memberships, so nothing they
    don't belong to can appear: an owner or admin sees every brand of the
    organization, anyone else only the brands they were added to, and the
    organization's brand and channel numbers count just those.
    """
    org = request.org
    org_roles = {m.organization_id: m.org_role for m in request.org_memberships}
    brand_roles = dict(
        WorkspaceMembership.objects.filter(user=request.user).values_list("workspace_id", "workspace_role")
    )
    managing_roles = (WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER)
    org_admin_roles = (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
    admin_org_ids = {org_id for org_id, role in org_roles.items() if role in org_admin_roles}
    api_key_org_ids = {m.organization_id for m in request.org_memberships if m.can_manage_api_keys}

    def group_for(organization):
        org_role = org_roles.get(organization.id)
        archived = billing_services.archived_plan(organization) if org_role is not None else None
        return {
            # Archived (its plan ended): the day its data is removed, else None.
            "archived_until": billing_services.archive_removal_at(archived) if archived else None,
            "organization": organization,
            "is_current": organization.id == org.id,
            "is_member": org_role is not None,
            # owner, admin or member; None for someone only added to a brand.
            "role": org_role,
            "can_edit": org_role in org_admin_roles,
            "can_delete": org_role == OrgMembership.OrgRole.OWNER,
            # What the page's filter reads: an organization waiting to be deleted is the inactive one.
            "status": "inactive" if organization.deletion_scheduled_for else "active",
            "archived_count": 0,
            "brands": [],
        }

    memberships = sorted(request.org_memberships, key=lambda m: m.organization.created_at, reverse=True)
    memberships.sort(key=lambda m: m.org_role not in org_admin_roles)
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
        .values_list("workspace_id")
        .annotate(total=Count("id"))
    )
    for organization_id, group in groups.items():
        group["member_count"] = len(people[organization_id])
        group["channel_count"] = 0
        group["brand_count"] = 0

    workspaces = (
        Workspace.objects.filter(Q(organization_id__in=admin_org_ids) | Q(id__in=brand_roles.keys()))
        .exclude(~Q(organization_id__in=org_roles.keys()), is_archived=True)
        .select_related("organization")
        .prefetch_related("memberships")
        .order_by("organization__name", "is_archived", "name")
    )
    workspaces = list(workspaces)
    active_brands = Counter(ws.organization_id for ws in workspaces if not ws.is_archived)
    for ws in workspaces:
        group = groups.get(ws.organization_id)
        if group is None:
            group = groups[ws.organization_id] = group_for(ws.organization)
        brand_role = brand_roles.get(ws.id)
        entry = {
            "workspace": ws,
            "is_member": brand_role is not None,
            "can_manage": brand_role in managing_roles or org_roles.get(ws.organization_id) in org_admin_roles,
            # The brand's API keys page needs the person in the brand, as every page of its settings does.
            "can_api_keys": brand_role is not None and ws.organization_id in api_key_org_ids and not ws.is_archived,
            # Out of an organization the person administers, when there is another to move it to.
            "can_move": ws.organization_id in admin_org_ids and len(admin_org_ids) > 1 and not ws.is_archived,
            # Archive, restore and delete are answered by the brand's settings,
            # which ask for an owner or manager of the brand itself.
            "can_close": brand_role in managing_roles,
            # The organization keeps at least one brand in use; only someone
            # who sees them all knows this is not the last.
            "is_last_active": not ws.is_archived
            and (ws.organization_id not in admin_org_ids or active_brands[ws.organization_id] <= 1),
        }
        if group["is_member"]:
            entry["member_count"] = len(ws.memberships.all())
        else:
            entry["role"] = brand_role
        group["brands"].append(entry)
        group["archived_count"] += ws.is_archived
        if group["is_member"] and not ws.is_archived:
            group["brand_count"] += 1
            group["channel_count"] += channels.get(ws.id, 0)

    org_groups = list(groups.values())
    # Position of each card under the "by name" order, applied in the page.
    by_name = sorted(org_groups, key=lambda g: (not g["is_member"], g["organization"].name.casefold()))
    for position, group in enumerate(by_name):
        group["name_position"] = position
    own = [g for g in org_groups if g["is_member"]]
    active = sum(g["status"] == "active" for g in org_groups)
    context = {
        "org_groups": org_groups,
        "filter_counts": {
            "all": len(org_groups),
            "active": active,
            "inactive": len(org_groups) - active,
            "archived": sum(g["archived_count"] for g in org_groups),
        },
        "totals": {
            "organizations": len(own),
            # Someone in two organizations is one person.
            "members": len(set().union(*people.values())) if people else 0,
            "brands": sum(g["brand_count"] for g in own),
            "channels": sum(g["channel_count"] for g in own),
        },
        # Where a brand can be moved to, for the move dialog (json_script).
        "move_destinations": [
            {"id": str(m.organization_id), "name": m.organization.name}
            for m in memberships
            if m.organization_id in admin_org_ids
        ],
        "settings_active": "workspaces",
    }
    return render(request, "organizations/workspaces.html", context)


def _administered_organization(request):
    """The organization named in the form, when the user owns or administers it; 403 otherwise.

    The organizations page lists several, so the form says which one.
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
        raise PermissionDenied("Only an owner or admin of the organization can change it.")
    return membership.organization


@login_required
@require_POST
def rename_view(request):
    """Rename one of the user's organizations."""
    _handle_name_update(request, _administered_organization(request))
    return redirect("organizations:workspaces")


@login_required
@require_POST
def edit_view(request):
    """Save the organization's page: its name and its billing data.

    Both or neither: the billing data goes first because the payment
    provider may refuse it, and a refusal must not leave the name changed
    under a message that says nothing was.
    """
    from apps.billing.views import save_payer

    organization = _administered_organization(request)
    name = clean_display_name(request.POST.get("name", ""))
    error = _("Organization name cannot be empty.") if not name else save_payer(organization, request.POST)
    if error:
        messages.error(request, error)
    else:
        organization.name = name
        organization.save(update_fields=["name"])
        messages.success(request, _("Organization updated."))
    return redirect("organizations:manage", organization_id=organization.id)


@login_required
@require_http_methods(["GET", "POST"])
def role_permissions_view(request, organization_id):
    """Account centre → Organizations → Roles and permissions of one organization.

    What each brand role may do in every brand of the organization. Only an
    owner or admin of that organization reaches it; anyone else gets a 404,
    so the page doesn't confirm the organization exists.
    """
    from apps.members import services as member_services

    membership = next(
        (
            m
            for m in request.org_memberships
            if m.organization_id == organization_id
            and m.org_role in (OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
        ),
        None,
    )
    if membership is None:
        raise Http404
    organization = membership.organization

    if request.method == "POST":
        if request.POST.get("action") == "reset":
            member_services.reset_role_permissions(organization)
            messages.success(request, _("The roles are back on the default permissions."))
        else:
            member_services.set_role_permissions(
                organization,
                {role: request.POST.getlist(f"role_{role}") for role in member_services.ADJUSTABLE_ROLES},
            )
            messages.success(request, _("Permissions saved."))
        return redirect("organizations:role_permissions", organization_id=organization.id)

    return render(
        request,
        "organizations/role_permissions.html",
        {
            "organization": organization,
            "roles": WorkspaceMembership.WorkspaceRole.choices,
            "matrix": member_services.role_permission_matrix(organization),
            "is_adjusted": bool(organization.role_permissions),
            "settings_active": "workspaces",
        },
    )


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
    """Schedule organization deletion for 14 days from now. The caller checks the owner."""
    from apps.organizations.tasks import execute_scheduled_org_deletion
    from apps.social_accounts.youtube_compliance import purge_organization

    org = type(org).objects.select_for_update().get(pk=org.pk)

    grace = timedelta(days=14)
    org.deletion_requested_at = django_tz.now()
    org.deletion_scheduled_for = django_tz.now() + grace
    org.save(update_fields=["deletion_requested_at", "deletion_scheduled_for"])
    purge_organization(org)

    execute_scheduled_org_deletion(str(org.id), schedule=grace)
    emails.send_deletion_scheduled(org, request.user)

    messages.success(request, _("Organization scheduled for deletion in 14 days."))


def _handle_immediate_org_deletion(request, org):
    """Permanently delete an organization right now. The caller checks the owner.

    When it was the owner's only organization their account goes with it
    (``Organization.hard_delete``): they are logged out and sent to sign-up
    to start fresh. An owner who keeps another organization stays signed
    in and lands on it.
    """
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
    """Cancel a pending organization deletion. The caller checks the owner.

    Also removes the queued background-task row so the worker isn't holding
    a no-op scheduled for 14 days out.
    """
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
