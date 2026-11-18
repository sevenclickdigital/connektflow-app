import uuid

from django.conf import settings
from django.db import models, transaction


class Organization(models.Model):
    #: The name every organization is created with at sign-up, until its
    #: owner gives it a real one on the welcome step.
    DEFAULT_NAME = "My Organization"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    logo_url = models.URLField(blank=True, default="")
    default_timezone = models.CharField(max_length=63, default="UTC")

    # Intelligence billing contact (the optional hosted SaaS integration).
    # Stripe Customer email defaults to this; falls back to the activating
    # user's email if blank. Stored on the Org (not the User) so a change
    # in activator doesn't affect where billing notices land — and so
    # invoices can be routed to a finance address rather than whoever
    # happened to click Subscribe.
    billing_email = models.EmailField(blank=True, default="")

    # Deletion workflow
    deletion_requested_at = models.DateTimeField(blank=True, null=True)
    deletion_scheduled_for = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations_organization"

    def __str__(self):
        return self.name

    @property
    def is_deletion_pending(self):
        return self.deletion_requested_at is not None and self.deleted_at is None

    @property
    def has_intelligence(self):
        """True when the org has an active Intelligence subscription AND
        the integration is enabled in this deployment.

        Returns False when ``settings.INTELLIGENCE_ENABLED`` is off even
        if a subscription row exists — so a self-hoster who removes the
        env vars doesn't accidentally keep the feature visible. Returns
        False when the related ``IntelligenceSubscription`` doesn't
        exist yet (apps.intelligence may not even be migrated on a
        deployment that hasn't opted in).

        The decorator ``@intelligence_subscription_required`` in
        apps.intelligence layers a stricter check (must be exactly
        ``status='active'``) for tool-call endpoints.
        """
        if not getattr(settings, "INTELLIGENCE_ENABLED", False):
            return False
        sub = getattr(self, "intelligence_subscription", None)
        return bool(sub and sub.status == "active")

    @property
    def is_unnamed(self):
        """True while the organization still has the name it was created with."""
        return self.name == self.DEFAULT_NAME

    def hard_delete(self, requesting_user=None):
        """Hard-delete this org and settle every member's account.

        - `requesting_user` (optional): the user who triggered the immediate-
          delete flow. When this was their only organization their account
          is deleted along with it, so they get a truly fresh start on
          re-signup; one who belongs to another organization keeps the
          account. Pass `None` (the default) for
          the scheduled-grace-period path and the management-command sweeper
          — those don't carry request context and we don't want to silently
          delete accounts from a background job.
        - Every other member gets a freshly provisioned default "My
          Organization" + "My brand" so they land on a working dashboard
          instead of limbo on next login.
        - Any user's `last_workspace_id` that points at a workspace in this
          org is nulled first (defensive; provision-for-member would reset it
          anyway, but the delete_user path wouldn't).
        """
        from apps.accounts.models import User
        from apps.accounts.signals import provision_organization_and_workspace
        from apps.media_library.services import schedule_stored_file_removal, tenant_stored_files
        from apps.members.models import OrgMembership
        from apps.workspaces.models import Workspace

        member_ids = list(OrgMembership.objects.filter(organization=self).values_list("user_id", flat=True))
        ws_ids = list(Workspace.objects.filter(organization=self).values_list("id", flat=True))

        if ws_ids:
            User.objects.filter(last_workspace_id__in=ws_ids).update(last_workspace_id=None)

        # The cascade removes rows only; the files are queued for removal in the
        # same transaction, from names read while the rows still exist.
        with transaction.atomic():
            schedule_stored_file_removal(tenant_stored_files(ws_ids, organization_id=self.pk))
            self.delete()  # CASCADE: workspaces, memberships, credentials, media, etc.

        requesting_user_id = requesting_user.pk if requesting_user else None
        for uid in member_ids:
            if uid == requesting_user_id and not OrgMembership.objects.filter(user_id=uid).exists():
                User.objects.filter(pk=uid).delete()
                continue
            try:
                user = User.objects.get(pk=uid)
            except User.DoesNotExist:
                continue
            provision_organization_and_workspace(user)
