"""Deleting a customer's account from the administrative area.

``delete_account`` removes the user and, in cascade, every organization they
are the only owner of (``Organization.hard_delete``: its workspaces, members,
media and files, with its Asaas subscriptions cancelled first). An organization
with another owner stays; the user only leaves it. The other members of a
deleted organization get a fresh one of their own, as hard_delete does for an
owner's own deletion.
"""

from __future__ import annotations

from django.db import transaction


def sole_owned_organizations(user):
    """Organizations where ``user`` is an owner and no one else is."""
    from apps.members.models import OrgMembership

    owned = OrgMembership.objects.filter(user=user, org_role=OrgMembership.OrgRole.OWNER).select_related("organization")
    return [
        membership.organization
        for membership in owned
        if not OrgMembership.objects.filter(organization=membership.organization, org_role=OrgMembership.OrgRole.OWNER)
        .exclude(user=user)
        .exists()
    ]


def delete_account(user):
    """Delete ``user`` and the organizations only they own. Returns those organizations' names.

    A gateway error while cancelling an organization's subscriptions
    propagates from hard_delete and stops here: that organization and the user
    are kept, and organizations already deleted stay deleted.
    """
    names = []
    for organization in sole_owned_organizations(user):
        name = organization.name
        organization.hard_delete(departing_user=user)
        names.append(name)
    with transaction.atomic():
        user.delete()
    return names
