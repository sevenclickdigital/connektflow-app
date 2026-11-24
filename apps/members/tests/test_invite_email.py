"""The invitation e-mail says what the person is invited to: an organization or one of its brands."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone, translation

from apps.members.models import Invitation, OrgMembership
from apps.members.services import _send_invite_email
from apps.workspaces.models import Workspace


def _invite(organization, inviter, **fields):
    return Invitation.objects.create(
        organization=organization,
        email="ana@example.com",
        name="Ana",
        invited_by=inviter,
        expires_at=timezone.now() + timedelta(days=7),
        **fields,
    )


def _sent(invitation):
    with translation.override("pt-br"):
        assert _send_invite_email(invitation)
    message = mail.outbox[-1]
    return message, message.alternatives[0][0]


@pytest.fixture
def camila(organization):
    organization.name = "CAMILA AP"
    organization.save(update_fields=["name"])
    return organization


@pytest.mark.django_db
def test_an_organization_invitation_names_the_organization(camila, org_owner):
    message, html = _sent(_invite(camila, org_owner, org_role=OrgMembership.OrgRole.ADMIN))

    assert message.subject == "Convite para a organização CAMILA AP no Connekt Flow"
    assert "Convite para a organização CAMILA AP" in html
    assert "convidou você para fazer parte da organização CAMILA AP no Connekt Flow" in message.body


@pytest.mark.django_db
def test_a_brand_invitation_names_the_brand_and_its_organization(camila, org_owner):
    brand = Workspace.objects.create(organization=camila, name="Loja Centro")
    invitation = _invite(
        camila,
        org_owner,
        org_role=OrgMembership.OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(brand.id), "role": "editor"}],
    )

    message, html = _sent(invitation)

    assert message.subject == "Convite para a marca Loja Centro no Connekt Flow"
    assert "Convite para a marca Loja Centro" in html
    assert "trabalhar na marca Loja Centro, da organização CAMILA AP, no Connekt Flow" in message.body


@pytest.mark.django_db
def test_an_invitation_whose_sender_is_gone_still_reads(camila):
    message, _html = _sent(_invite(camila, None))

    assert message.body.count("Alguém da equipe convidou você") == 1
