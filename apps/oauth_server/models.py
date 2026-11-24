"""The brand an OAuth (MCP) grant was given for.

A person approves a client while one brand is open; everything that grant
leads to — the authorization code, each access token and each refresh token
the rotation mints — acts on that brand only, whatever they open in the
browser afterwards. One row per credential, so the binding is read with the
token itself and is copied forward when a code is exchanged or a refresh
token rotates (``apps.oauth_server.validator``).

A credential with no row predates the binding and keeps following the user's
last opened brand (``apps.oauth_server.services.oauth_membership``).

The links to django-oauth-toolkit's tables carry no database constraint: a
release that does not know this table can still delete grants and tokens
while it is being deployed, and the ORM cascade cleans up after it in the
releases that do. Ids of those tables are never reused, so an orphaned row
can never be read as another credential's binding.
"""

from __future__ import annotations

from django.db import models
from oauth2_provider.settings import oauth2_settings


class OAuthWorkspaceBinding(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="oauth_bindings",
    )
    grant = models.OneToOneField(
        oauth2_settings.GRANT_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="workspace_binding",
    )
    access_token = models.OneToOneField(
        oauth2_settings.ACCESS_TOKEN_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="workspace_binding",
    )
    refresh_token = models.OneToOneField(
        oauth2_settings.REFRESH_TOKEN_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="workspace_binding",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "oauth_server_workspace_binding"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(grant__isnull=False, access_token__isnull=True, refresh_token__isnull=True)
                    | models.Q(grant__isnull=True, access_token__isnull=False, refresh_token__isnull=True)
                    | models.Q(grant__isnull=True, access_token__isnull=True, refresh_token__isnull=False)
                ),
                name="oauth_binding_exactly_one_credential",
            )
        ]

    def __str__(self) -> str:
        return f"OAuth binding to workspace {self.workspace_id}"
