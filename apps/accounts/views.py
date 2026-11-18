import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render, resolve_url
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts import impersonation
from apps.accounts.forms import ProfileForm
from apps.billing import services as billing_services

logger = logging.getLogger(__name__)


def health_check(request):
    """Health check endpoint at /health/."""
    return JsonResponse({"status": "ok"})


@login_required
def dashboard(request):
    """Home: the last used brand's dashboard, or the org overview when there is none."""
    from apps.members.models import WorkspaceMembership

    user = request.user

    # Only trust last_workspace_id if the user still has an active membership
    # in that workspace. Otherwise the org may have been deleted out from under
    # them and we'd redirect into a 403.
    if user.last_workspace_id:
        if WorkspaceMembership.objects.filter(
            user=user,
            workspace_id=user.last_workspace_id,
            workspace__is_archived=False,
        ).exists():
            # A new account starts by naming its organization and brand (the
            # welcome step of the contracting page), whether or not unpaid
            # brands are locked. Once the brand is in use it is not asked again.
            org = request.org
            if (
                org is not None
                and org.is_unnamed
                and request.org_membership.org_role == "owner"
                and request.workspace is not None
                and request.workspace.organization_id == org.id
                and not billing_services.is_workspace_active(request.workspace)
            ):
                return redirect("billing:subscribe_modal", workspace_id=user.last_workspace_id)
            return redirect("workspaces:dashboard", workspace_id=user.last_workspace_id)
        user.last_workspace_id = None
        user.save(update_fields=["last_workspace_id"])

    # Fallback: try to find any workspace the user belongs to
    membership = (
        WorkspaceMembership.objects.filter(user=user, workspace__is_archived=False).select_related("workspace").first()
    )
    if membership:
        user.last_workspace_id = membership.workspace.id
        user.save(update_fields=["last_workspace_id"])
        return redirect("workspaces:dashboard", workspace_id=membership.workspace.id)

    # An operator account owns nothing on purpose — no organization, no
    # workspace, so nothing to bill (see
    # apps/accounts/management/commands/create_operator.py). The
    # "create a workspace" screen below is therefore exactly wrong for
    # them: it is an invitation to become a customer, which is the
    # coupling the separate operator account exists to undo. Send them to
    # the console they logged in to use.
    #
    # Checked through ``impersonation.is_operator`` rather than
    # ``user.is_staff``: an operator impersonating a customer who happens
    # to have no workspace must see what that customer sees, not be
    # bounced to a console their session may not open.
    if impersonation.is_operator(request):
        return redirect("billing_staff:staff_console")

    return render(request, "accounts/dashboard.html")


@login_required
@require_http_methods(["GET", "POST"])
def account_settings(request):
    user = request.user
    settings_active = "profile"

    profile_form = ProfileForm(instance=user)
    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "update_photo":
            _handle_photo_update(request, user)
        elif action == "update_profile":
            profile_form = ProfileForm(request.POST, instance=user)
            if not profile_form.is_valid():
                # Re-rendered, not redirected: the errors sit next to the fields.
                return _render_account_settings(request, user, settings_active, profile_form)
            profile_form.save()
            messages.success(request, _("Profile updated."))
        elif action == "update_password":
            _handle_password_update(request, user)
        elif action == "delete_account":
            return _handle_account_deletion(request, user)

        return redirect("accounts:settings")

    return _render_account_settings(request, user, settings_active, profile_form)


def _render_account_settings(request, user, settings_active, profile_form):
    # The membership in the organization in use, for the role shown on the page.
    org_membership = request.org_membership
    return render(
        request,
        "accounts/settings.html",
        {
            "settings_active": settings_active,
            "org_membership": org_membership,
            "profile_form": profile_form,
        },
    )


def _handle_photo_update(request, user):
    """Handle avatar upload or deletion."""
    # Handle deletion
    if request.POST.get("delete_photo") == "1":
        if user.avatar:
            user.avatar.delete(save=False)
        # Named fields only: this instance was loaded at the start of the
        # request, and a full save would write back any column another request
        # changed meanwhile (e.g. review_banner_dismissed_at).
        user.save(update_fields=["avatar", "updated_at"])
        messages.success(request, _("Photo removed."))
        return

    # Handle upload
    if "avatar" not in request.FILES:
        return

    avatar = request.FILES["avatar"]

    # The type comes from the file's bytes and the stored name gets the
    # matching extension: avatars/ is served publicly, by suffix.
    from apps.media_library.validators import image_storage_filename

    storage_name = image_storage_filename(avatar)
    if not storage_name:
        messages.error(request, _("Photo must be a JPEG, PNG, WebP, or GIF image."))
        return

    # Validate file size (2 MB max)
    max_size = 2 * 1024 * 1024
    if avatar.size > max_size:
        messages.error(request, _("Photo must be under 2 MB."))
        return

    # Validate minimum dimensions (180x180)
    #
    # PIL is imported here rather than at module scope because config.urls
    # imports this module for ``health_check``, so a top-level import loaded
    # Pillow's _imaging extension into every web AND worker process at boot,
    # whether or not an avatar was ever touched. Matches how the rest of the
    # codebase imports PIL (apps.media_library.services, apps.intelligence.views).
    from PIL import Image

    try:
        with Image.open(avatar) as img:
            width, height = img.size
        if width < 180 or height < 180:
            messages.error(request, _("Photo must be at least 180×180 pixels."))
            return
    except Exception:
        messages.error(request, _("Could not read image file."))
        return
    finally:
        avatar.seek(0)  # Reset file pointer after reading

    # Delete old avatar before saving new one
    if user.avatar:
        user.avatar.delete(save=False)

    avatar.name = storage_name
    user.avatar = avatar
    user.save(update_fields=["avatar", "updated_at"])
    messages.success(request, _("Photo updated."))


def _handle_password_update(request, user):
    """Handle password change."""
    current_password = request.POST.get("current_password", "")
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")

    if not current_password:
        messages.error(request, _("Current password is required."))
        return

    if not user.check_password(current_password):
        messages.error(request, _("Current password is incorrect."))
        return

    if not password:
        messages.error(request, _("New password cannot be empty."))
        return

    if len(password) < 8:
        messages.error(request, _("New password must be at least 8 characters."))
        return

    if password != password_confirm:
        messages.error(request, _("New passwords do not match."))
        return

    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    update_session_auth_hash(request, user)
    messages.success(request, _("Password changed."))
    # This form does not go through allauth, so neither its security notice nor
    # its password_changed signal (apps.accounts.signals) fires on its own.
    _send_password_changed_notice(request, user)
    from apps.oauth_server.services import revoke_user_tokens

    if revoke_user_tokens(user):
        messages.info(
            request, _("The apps connected to your account were disconnected. Authorize them again to keep using them.")
        )


def _send_password_changed_notice(request, user):
    """Email the owner that the password changed, as allauth does on its own forms.

    The password is already saved, so a failed send is logged and the change
    stands: holding it back would not make the account any safer.
    """
    from allauth.account.adapter import get_adapter

    try:
        get_adapter(request).send_notification_mail("account/email/password_changed", user)
    except Exception:
        logger.exception("Could not send the password-changed notice to user %s", user.pk)


def _handle_account_deletion(request, user):
    """Handle account deletion with sole-owner safety check."""
    from apps.members.models import OrgMembership

    # Check if user is the sole owner of any organization
    owned_memberships = OrgMembership.objects.filter(user=user, org_role=OrgMembership.OrgRole.OWNER).select_related(
        "organization"
    )

    sole_owner_orgs = []
    for membership in owned_memberships:
        other_owners = (
            OrgMembership.objects.filter(
                organization=membership.organization,
                org_role=OrgMembership.OrgRole.OWNER,
            )
            .exclude(user=user)
            .exists()
        )
        if not other_owners:
            sole_owner_orgs.append(membership.organization.name)

    if sole_owner_orgs:
        org_names = ", ".join(sole_owner_orgs)
        messages.error(
            request,
            _(
                "You are the sole owner of: %(orgs)s. "
                "Transfer ownership or delete the organization before deleting your account."
            )
            % {"orgs": org_names},
        )
        return redirect("accounts:settings")

    # Safe to delete
    user.delete()
    logout(request)
    messages.success(request, _("Your account has been deleted."))
    return redirect("account_login")


@login_required
def login_popup_done(request):
    """Last page of a sign-in that ran in a popup.

    The login and signup pages send the Google round trip into a small window
    and name this address as where it ends. The page tells the window that
    opened the popup to carry on, now signed in, and closes itself (see
    social_accounts/connect_done.html, shared with the account connections).
    """
    next_url = request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = resolve_url(settings.LOGIN_REDIRECT_URL)
    return render(
        request,
        "social_accounts/connect_done.html",
        {"next_url": next_url, "title": _("You are signed in")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def accept_terms(request):
    """Terms of Service acceptance page for social signup users."""
    if request.user.tos_accepted_at is not None:
        return redirect("/")

    if request.method == "POST":
        if request.POST.get("agree"):
            request.user.tos_accepted_at = timezone.now()
            request.user.save(update_fields=["tos_accepted_at"])
            return redirect("/")
        messages.error(request, _("You must agree to the Terms of Service and Privacy Policy to continue."))

    return render(request, "account/accept_terms.html")


def logout_view(request):
    logout(request)
    return redirect("account_login")


@login_required
@require_POST
def dismiss_review_banner(request):
    """Hide the sidebar's Trustpilot card until the next login."""
    request.user.review_banner_dismissed_at = timezone.now()
    request.user.save(update_fields=["review_banner_dismissed_at"])
    return HttpResponse(status=204)
