"""Creating brands (workspaces) and moving them between organizations."""

from __future__ import annotations

from django.db import transaction
from django.utils.translation import gettext as _

from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.workspaces.models import Workspace


def create_brand(organization, name, owner):
    """A new brand of ``organization``, owned by ``owner``, with its billing row.

    The billing row (``ensure_subscription``) exists from the start because the
    staff console and the storage quota read it.
    """
    from apps.billing import services as billing_services

    with transaction.atomic():
        workspace = Workspace.objects.create(organization=organization, name=name)
        WorkspaceMembership.objects.create(
            user=owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        billing_services.ensure_subscription(workspace)
    return workspace


# Session key holding the id of the brand the person has just created, while
# its checkout may still give up on it.
NEW_BRAND_SESSION_KEY = "new_brand_id"


class BrandMoveError(Exception):
    """The brand cannot be moved; the message says why, for the person who asked."""


def movable_organization_ids(user):
    """The organizations ``user`` may move brands out of and into: owner or admin."""
    return set(
        OrgMembership.objects.filter(
            user=user, org_role__in=(OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
        ).values_list("organization_id", flat=True)
    )


def move_brand(workspace, destination):
    """Move ``workspace`` to the organization ``destination``, with everything it holds.

    What belongs to the brand goes with it untouched: posts, channels, inbox,
    automations, analytics, contacts, media, API keys and the people linked to
    it (someone who is not in the destination organization stays linked to the
    brand alone). What belongs to the organization it leaves stays there:

    - **Billing.** The payer is the organization. A brand paid on a gateway
      subscription of its own, or with extra disk charged there, is refused
      (:class:`BrandMoveError`): the charge would keep going to the
      organization it left. A brand covered by the
      organization's plan gives its place back, and the destination's plan
      covers it if it has a brand left. An exempt brand stays exempt only when
      the destination has an exemption unused, so moving is not a way around
      an exemption's limit or end date; otherwise it arrives ``pending``.
    - **Custom roles** are the organization's, so each person falls back to
      the standard role they also hold in the brand.
    - **Pending invitations** of the organization stop naming the brand.

    The caller checks that the person may act on both organizations
    (:func:`movable_organization_ids`).
    """
    from apps.billing import services as billing_services
    from apps.billing.models import WorkspaceAddon, WorkspaceSubscription
    from apps.media_library.models import MediaAsset, MediaFolder, PendingUpload

    status = WorkspaceSubscription.Status
    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        origin_id = workspace.organization_id
        if origin_id == destination.pk:
            raise BrandMoveError(_("The brand is already in this organization."))
        if workspace.is_archived:
            raise BrandMoveError(_("Restore the brand before moving it."))
        if destination.deletion_requested_at:
            raise BrandMoveError(_("That organization is scheduled for deletion. Choose another one."))

        subscription = billing_services.ensure_subscription(workspace)
        if (
            subscription.gateway_subscription_id
            and not subscription.plan_id
            and subscription.status in (status.ACTIVE, status.PAST_DUE)
        ):
            raise BrandMoveError(
                _(
                    "This brand has a subscription of its own, charged to the organization it is in. "
                    "Cancel it on the brand's billing page before moving the brand."
                )
            )

        if WorkspaceAddon.objects.filter(workspace=workspace, status=WorkspaceAddon.Status.ACTIVE).exists():
            raise BrandMoveError(
                _(
                    "This brand has extra disk charged to the organization it is in. "
                    "Cancel it on the brand's billing page before moving the brand."
                )
            )

        was_exempt = subscription.status == status.COURTESY
        origin_plan = subscription.plan
        if origin_plan is not None:
            subscription.plan = None
            subscription.status = status.PENDING
            subscription.save(update_fields=["plan", "status", "updated_at"])

        workspace.organization = destination
        workspace.save(update_fields=["organization", "updated_at"])
        for model in (MediaAsset, MediaFolder, PendingUpload):
            model.objects.filter(workspace=workspace).update(organization=destination)
        WorkspaceMembership.objects.filter(workspace=workspace, custom_role__isnull=False).update(custom_role=None)
        _drop_from_pending_invitations(origin_id, workspace)

        if was_exempt:
            # Counted after the move, so the brand itself is one of the used ones.
            profile = billing_services.ensure_billing_profile(destination)
            if profile.courtesy_workspaces_used > profile.courtesy_workspace_quota:
                subscription.status = status.PENDING
                subscription.courtesy_note = ""
                subscription.save(update_fields=["status", "courtesy_note", "updated_at"])
        if origin_plan is not None:
            billing_services.sync_plan_coverage(origin_plan)
        destination_plan = billing_services.plan_for(destination)
        if destination_plan is not None:
            billing_services.sync_plan_coverage(destination_plan)
    return workspace


def _drop_from_pending_invitations(organization_id, workspace):
    """Take the brand out of the organization's invitations not accepted yet."""
    brand_id = str(workspace.pk)
    for invitation in Invitation.objects.filter(organization_id=organization_id, accepted_at__isnull=True):
        kept = [a for a in invitation.workspace_assignments if str(a.get("workspace_id")) != brand_id]
        if len(kept) != len(invitation.workspace_assignments):
            invitation.workspace_assignments = kept
            invitation.save(update_fields=["workspace_assignments"])


def discard_unused_brand(workspace):
    """Delete a brand that was created and never used. Returns whether it was deleted.

    For someone who gives up on a brand at its checkout. Only a brand with
    nothing to lose goes: never contracted, exempted or put on trial, with no
    post, channel or media, and not the only brand of its organization (an
    organization always has one). Anything else is kept.
    """
    from apps.billing import services as billing_services
    from apps.billing.models import WorkspaceSubscription
    from apps.composer.models import Post
    from apps.media_library.models import MediaAsset
    from apps.social_accounts.models import SocialAccount

    with transaction.atomic():
        subscription = billing_services.ensure_subscription(workspace)
        untouched = (
            subscription.status == WorkspaceSubscription.Status.PENDING
            and subscription.trial_ends_at is None
            and subscription.plan_id is None
            and not subscription.gateway_subscription_id
        )
        if not untouched:
            return False
        if any(model.objects.filter(workspace=workspace).exists() for model in (Post, SocialAccount, MediaAsset)):
            return False
        siblings = Workspace.objects.filter(organization_id=workspace.organization_id, is_archived=False)
        if not siblings.exclude(pk=workspace.pk).exists():
            return False
        workspace.delete()
    return True
