import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from apps.common.managers import OrgScopedManager


class OrgMembership(models.Model):
    class OrgRole(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_memberships",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    org_role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(blank=True, null=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_org_membership"
        unique_together = [("user", "organization")]

    def __str__(self):
        return f"{self.user.email} - {self.organization.name} ({self.org_role})"

    @property
    def can_manage_api_keys(self):
        """Whether this role issues and revokes the organization's API keys (read by the menus)."""
        return has_org_permission(self, "manage_api_keys")


class WorkspaceMembership(models.Model):
    class WorkspaceRole(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        EDITOR = "editor", "Editor"
        CONTRIBUTOR = "contributor", "Contributor"
        CLIENT = "client", "Client"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    workspace_role = models.CharField(
        max_length=20,
        choices=WorkspaceRole.choices,
        default=WorkspaceRole.VIEWER,
    )
    custom_role = models.ForeignKey(
        "CustomRole",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="memberships",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "members_workspace_membership"
        unique_together = [("user", "workspace")]

    def __str__(self):
        role = self.custom_role.name if self.custom_role else self.workspace_role
        return f"{self.user.email} - {self.workspace.name} ({role})"

    @property
    def effective_permissions(self):
        """Return the effective permission dict for this membership.

        The role's built-in permissions with what the brand's organization
        changed in that role (``Organization.role_permissions``). The owner
        role is never adjusted, so a brand always has someone who can do
        everything in it.
        """
        if self.custom_role:
            return self.custom_role.permissions
        builtin = BUILTIN_ROLE_PERMISSIONS.get(self.workspace_role, {})
        if self.workspace_role == self.WorkspaceRole.OWNER:
            return builtin
        adjusted = (self._organization_role_permissions or {}).get(self.workspace_role)
        if not adjusted:
            return builtin
        return {key: bool(adjusted.get(key, granted)) for key, granted in builtin.items()}

    @cached_property
    def _organization_role_permissions(self):
        """The role adjustments of the organization that owns the brand.

        Taken from the workspace when it came loaded with its organization
        (as the request's membership does); one query otherwise.
        """
        workspace = self._state.fields_cache.get("workspace")
        if workspace is not None and "organization" in workspace._state.fields_cache:
            return workspace.organization.role_permissions
        from apps.organizations.models import Organization

        return (
            Organization.objects.filter(workspaces=self.workspace_id).values_list("role_permissions", flat=True).first()
        )


class CustomRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="custom_roles",
    )
    name = models.CharField(max_length=100)
    permissions = models.JSONField(
        default=dict,
        help_text="Permission keys mapped to booleans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_custom_role"
        unique_together = [("organization", "name")]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


def _generate_invitation_token():
    return secrets.token_urlsafe(32)


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    # How the inviter called the person: greets them in the e-mail and becomes
    # their name when the account they accept with has none.
    name = models.CharField(max_length=255, blank=True, default="", db_default="")
    org_role = models.CharField(
        max_length=20,
        choices=OrgMembership.OrgRole.choices,
        default=OrgMembership.OrgRole.MEMBER,
    )
    workspace_assignments = models.JSONField(
        default=list,
        help_text='List of {"workspace_id": "...", "role": "..."}',
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    token = models.CharField(max_length=255, unique=True, default=_generate_invitation_token)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # How many times this address has been mailed about this invitation, and
    # when it last was. On the row rather than in the cache on purpose: the
    # cache is LocMemCache whenever REDIS_URL is unset, so a deploy would hand
    # back a fresh allowance. The same reasoning as ApprovalReminder's
    # reminder_count/last_reminder_at, which this mirrors.
    send_count = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(blank=True, null=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_invitation"

    def __str__(self):
        return f"Invitation to {self.email} for {self.organization.name}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @cached_property
    def brand(self):
        """The brand a brand invitation is for, else None.

        An invitation sent from a brand's team page: member of the
        organization with access to that one brand. It is worded around the
        brand, which is all the person was invited to work in.
        """
        if self.org_role != OrgMembership.OrgRole.MEMBER or len(self.workspace_assignments) != 1:
            return None
        from apps.workspaces.models import Workspace

        return Workspace.objects.filter(
            id=self.workspace_assignments[0]["workspace_id"], organization_id=self.organization_id
        ).first()

    @property
    def team_name(self):
        """What the person is told they are joining: the brand, or the organization."""
        return self.brand.name if self.brand else self.organization.name

    @property
    def is_accepted(self):
        return self.accepted_at is not None


# Built-in workspace role permission mappings
PERMISSION_KEYS = [
    "create_posts",
    "edit_others_posts",
    "approve_posts",
    "publish_directly",
    "manage_social_accounts",
    "view_analytics",
    "use_inbox",
    "reply_from_inbox",
    "manage_workspace_settings",
    "upload_media",
    "edit_media",
    "delete_media",
    "manage_media",
]

#: The name of each permission on the "Roles and permissions" page.
PERMISSION_LABELS = {
    "create_posts": _("Create posts"),
    "edit_others_posts": _("Edit other people's posts"),
    "approve_posts": _("Approve posts"),
    "publish_directly": _("Schedule and publish without approval"),
    "manage_social_accounts": _("Connect and manage channels"),
    "view_analytics": _("View analytics"),
    "use_inbox": _("Open the inbox"),
    "reply_from_inbox": _("Reply from the inbox and manage automations"),
    "manage_workspace_settings": _("Manage saved replies, the inbox SLA and funnels"),
    "upload_media": _("Upload media"),
    "edit_media": _("Edit media"),
    "delete_media": _("Delete media"),
    "manage_media": _("Organize the media library"),
}

BUILTIN_ROLE_PERMISSIONS = {
    "owner": {k: True for k in PERMISSION_KEYS},
    "manager": {
        "create_posts": True,
        "edit_others_posts": True,
        "approve_posts": True,
        "publish_directly": True,
        "manage_social_accounts": True,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": True,
        "manage_workspace_settings": False,
        "upload_media": True,
        "edit_media": True,
        "delete_media": True,
        "manage_media": True,
    },
    "editor": {
        "create_posts": True,
        "edit_others_posts": True,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": True,
        "manage_workspace_settings": False,
        "upload_media": True,
        "edit_media": True,
        "delete_media": True,
        "manage_media": False,
    },
    "contributor": {
        "create_posts": True,
        "edit_others_posts": False,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": False,
        "use_inbox": False,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": True,
        "edit_media": True,
        "delete_media": False,
        "manage_media": False,
    },
    "client": {
        "create_posts": False,
        "edit_others_posts": False,
        "approve_posts": True,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": False,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": False,
        "edit_media": False,
        "delete_media": False,
        "manage_media": False,
    },
    "viewer": {
        "create_posts": False,
        "edit_others_posts": False,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": False,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": False,
        "edit_media": False,
        "delete_media": False,
        "manage_media": False,
    },
}


# ---------------------------------------------------------------------------
# Org-level permission model
# ---------------------------------------------------------------------------
# The workspace permission system above is workspace-scoped via
# ``WorkspaceMembership.effective_permissions``. The Intelligence integration
# needs ORG-scoped permission checks (subscriptions and Stripe billing are
# tied to the Organization, not any one workspace). Rather than expand the
# workspace system to do double duty, we introduce a parallel, narrower
# org-permission table keyed on ``OrgMembership.org_role``.
#
# Add new keys here as future features need org-scoped gating.

ORG_PERMISSION_KEYS = (
    ("manage_intelligence_billing", "Manage Intelligence subscription + billing"),
    ("use_intelligence", "Use Intelligence tools"),
    ("manage_api_keys", "Issue and revoke Agent API keys for any workspace in the org"),
)


BUILTIN_ORG_PERMISSIONS = {
    OrgMembership.OrgRole.OWNER: {
        "manage_intelligence_billing",
        "use_intelligence",
        "manage_api_keys",
    },
    OrgMembership.OrgRole.ADMIN: {
        "manage_intelligence_billing",
        "use_intelligence",
        "manage_api_keys",
    },
    OrgMembership.OrgRole.MEMBER: {
        "use_intelligence",
    },
}


def has_org_permission(membership, key):
    """Return True if ``membership`` grants the given org-permission key.

    ``membership`` is an ``OrgMembership`` or None (e.g., the user has no
    membership in the org being checked). None always returns False.
    """
    if membership is None:
        return False
    return key in BUILTIN_ORG_PERMISSIONS.get(membership.org_role, set())
