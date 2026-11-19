"""The "about your business" step a new account is asked once, before its plan."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization


def _signed_up(client, email="dona@mac.example"):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    client.force_login(user)
    return user, WorkspaceMembership.objects.get(user=user).workspace


def _about_url(workspace):
    return f"{reverse('organizations:about')}?workspace={workspace.id}"


@pytest.mark.django_db
def test_a_new_account_is_asked_about_its_business_before_the_plan(client):
    _user, workspace = _signed_up(client)

    landing = client.get(reverse("dashboard"), follow=True)

    assert landing.redirect_chain[-1][0] == _about_url(workspace)
    assert landing.status_code == 200


@pytest.mark.django_db
def test_the_answers_are_kept_and_the_plan_comes_next(client):
    _user, workspace = _signed_up(client)

    response = client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "action": "save",
            "business_activity": "agency",
            "team_size": "2_5",
            "referral_source": "instagram",
            "goals": ["schedule", "reports"],
        },
    )

    assert response.headers["Location"] == reverse("billing:checkout", kwargs={"workspace_id": workspace.id})
    organization = Organization.objects.get(pk=workspace.organization_id)
    assert (organization.business_activity, organization.team_size, organization.referral_source) == (
        "agency",
        "2_5",
        "instagram",
    )
    assert organization.goals == ["schedule", "reports"]
    assert organization.about_answered_at is not None
    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id})).status_code == 200


@pytest.mark.django_db
def test_skipping_closes_the_step_without_answers(client):
    _user, workspace = _signed_up(client)

    client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "action": "skip", "business_activity": "agency"},
    )

    organization = Organization.objects.get(pk=workspace.organization_id)
    assert organization.about_answered_at is not None
    assert organization.business_activity == ""
    assert client.get(_about_url(workspace)).headers["Location"] == reverse("dashboard")


@pytest.mark.django_db
def test_an_answer_outside_the_choices_is_refused(client):
    _user, workspace = _signed_up(client)

    response = client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "action": "save", "business_activity": "astronaut", "goals": ["fly"]},
    )

    assert response.status_code == 200
    organization = Organization.objects.get(pk=workspace.organization_id)
    assert organization.about_answered_at is None
    assert organization.business_activity == ""


@pytest.mark.django_db
def test_nobody_answers_for_someone_elses_organization(client):
    _owner, workspace = _signed_up(client)
    intruder = User.objects.create_user(email="x@y.example", password="x", tos_accepted_at=timezone.now())
    client.force_login(intruder)

    client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "action": "save", "business_activity": "agency"},
    )

    organization = Organization.objects.get(pk=workspace.organization_id)
    assert organization.about_answered_at is None
    assert organization.business_activity == ""


@pytest.mark.django_db
def test_an_existing_account_is_not_asked(client):
    _user, workspace = _signed_up(client)
    Organization.objects.filter(pk=workspace.organization_id).update(name="Padaria Sol")

    response = client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id}))

    assert response.status_code == 200
