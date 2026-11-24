"""The e-mail sent when an owner schedules the deletion of an organization."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone, translation

from apps.organizations.emails import DELETION_NOTICE_MAX_BRANDS, send_deletion_scheduled
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def scheduled(organization):
    organization.name = "Camila AP"
    organization.deletion_requested_at = timezone.now()
    organization.deletion_scheduled_for = timezone.now() + timedelta(days=14)
    organization.save()
    return organization


def _send(org, user, django_capture_on_commit_callbacks, **kwargs):
    with translation.override("pt-br"), django_capture_on_commit_callbacks(execute=True):
        send_deletion_scheduled(org, user, **kwargs)
    message = mail.outbox[-1]
    return message, message.alternatives[0][0]


@pytest.mark.django_db
def test_it_says_an_organization_goes_and_names_its_brands(scheduled, org_owner, django_capture_on_commit_callbacks):
    Workspace.objects.create(organization=scheduled, name="Loja Centro")
    Workspace.objects.create(organization=scheduled, name="Ateliê")

    message, html = _send(scheduled, org_owner, django_capture_on_commit_callbacks)

    assert message.subject.startswith("A organização Camila AP será excluída em ")
    assert "Você pediu a exclusão da organização" in message.body
    assert "suas marcas: Ateliê, Loja Centro" in message.body
    assert "Ateliê, Loja Centro" in html


@pytest.mark.django_db
def test_youtube_is_mentioned_only_with_the_channels_removed(scheduled, org_owner, django_capture_on_commit_callbacks):
    brand = Workspace.objects.create(organization=scheduled, name="Loja")
    SocialAccount.objects.create(workspace=brand, platform="instagram_login", account_platform_id="ig1")
    SocialAccount.objects.create(workspace=brand, platform="facebook", account_platform_id="fb1")

    without, _html = _send(scheduled, org_owner, django_capture_on_commit_callbacks)
    with_channel, _html = _send(
        scheduled, org_owner, django_capture_on_commit_callbacks, removed_youtube=["Canal da Camila"]
    )

    assert "YouTube" not in without.body
    assert "o canal do YouTube Canal da Camila foi desconectado na hora" in with_channel.body
    # The networks still connected keep working until the date, each named once.
    assert "outras contas conectadas (Facebook, Instagram) continuam funcionando" in without.body


@pytest.mark.django_db
def test_a_long_brand_list_is_cut_and_counted(scheduled, org_owner, django_capture_on_commit_callbacks):
    for i in range(DELETION_NOTICE_MAX_BRANDS + 3):
        Workspace.objects.create(organization=scheduled, name=f"Marca {i:02d}")

    message, _html = _send(scheduled, org_owner, django_capture_on_commit_callbacks)

    assert "Marca 09 e mais 3" in message.body
    assert "Marca 10" not in message.body


@pytest.mark.django_db
def test_the_heading_is_beside_the_mascot_and_again_for_phones(
    scheduled, org_owner, django_capture_on_commit_callbacks
):
    _message, html = _send(scheduled, org_owner, django_capture_on_commit_callbacks)

    headings = re.findall(r'<h1 class="cf-h1[^"]*"[^>]*>(.*?)</h1>', html, re.S)
    assert headings == ["A organização Camila AP será excluída"] * 2
    assert 'class="cf-h1 cf-h1-desktop"' in html
    assert 'class="cf-h1-mobile"' in html
