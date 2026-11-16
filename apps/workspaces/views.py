from zoneinfo import available_timezones

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.billing import services
from apps.members.models import OrgMembership, WorkspaceMembership

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

    workspace = Workspace.objects.create(
        organization=membership.organization,
        name=name,
    )

    WorkspaceMembership.objects.create(
        user=request.user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as current workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])

    # A new workspace is the billable unit, so the next screen is the
    # contracting modal rather than the calendar. ``ensure_subscription``
    # runs here (not only in the modal) so the row exists even if the
    # creator closes the tab — the staff console and the storage quota
    # both read it, and a workspace with no row at all reads as having no
    # quota at all.
    services.ensure_subscription(workspace)
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
        action = request.POST.get("action")

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
                workspace.delete()
            messages.success(
                request, _('Workspace "%(name)s" has been permanently deleted.') % {"name": workspace_name}
            )
            return redirect("organizations:workspaces")

        if action == "update_timezone" and is_owner_or_manager:
            tz = request.POST.get("timezone", "").strip()
            if tz not in available_timezones():
                messages.error(request, _("Invalid timezone."))
            else:
                workspace.timezone = tz
                workspace.save(update_fields=["timezone"])
                messages.success(request, _("Timezone updated."))
            return redirect("workspaces:settings", workspace_id=workspace.id)

        if action == "update_segments" and is_owner_or_manager:
            from django.core.exceptions import ValidationError

            from apps.calendar.services import set_date_segments

            try:
                set_date_segments(workspace, request.POST.getlist("segments"))
            except (ValidationError, ValueError):
                messages.error(request, _("Invalid segment."))
            else:
                messages.success(request, _("Segments updated."))
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

            # Validate file type
            allowed_types = ("image/jpeg", "image/png", "image/webp", "image/gif")
            if icon.content_type not in allowed_types:
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
        valid_modes = Workspace.ApprovalWorkflowMode.values
        if mode not in valid_modes:
            messages.error(request, _("Invalid approval workflow mode."))
            return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

        workspace.approval_workflow_mode = mode
        workspace.save(update_fields=["approval_workflow_mode", "updated_at"])
        messages.success(request, _("Approval workflow updated."))
        return redirect("workspaces:approvals_settings", workspace_id=workspace.id)

    return render(
        request,
        "workspaces/approvals_settings.html",
        {
            "workspace": workspace,
            "settings_active": "approvals",
            "is_owner_or_manager": is_owner_or_manager,
            "approval_modes": Workspace.ApprovalWorkflowMode,
        },
    )


@login_required
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
                    org_role=OrgMembership.OrgRole.MEMBER,
                    workspace_assignments=[{"workspace_id": str(workspace.id), "role": role}],
                    invited_by=request.user,
                    inviter=request.user,
                )
                messages.success(request, _("Invitation sent."))
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
    return render(
        request,
        "workspaces/team.html",
        {
            "workspace": workspace,
            "memberships": memberships,
            "addable_members": addable,
            "role_choices": assignable,
            "assignable_values": [value for value, _label in assignable],
            "all_roles": WorkspaceMembership.WorkspaceRole.choices,
            "settings_active": "team",
        },
    )
