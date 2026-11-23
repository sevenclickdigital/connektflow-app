"""The "about your business" step a new account answers once, before its plan,
and the checkout's billing details worded after it."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services as billing
from apps.billing.models import WorkspaceSubscription
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
            "client_count": "6_15",
            "organization_name": "  Agência   Sol ",
        },
    )

    assert response.headers["Location"] == reverse("billing:checkout", kwargs={"workspace_id": workspace.id})
    organization = Organization.objects.get(pk=workspace.organization_id)
    assert (organization.business_activity, organization.team_size, organization.referral_source) == (
        "agency",
        "2_5",
        "instagram",
    )
    assert organization.name == "Agência Sol"
    assert organization.about_answered_at is not None
    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id})).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("source", "kept"),
    [("other", "Podcast do João"), ("instagram", "")],
)
def test_where_else_is_kept_only_with_the_somewhere_else_source(client, source, kept):
    _user, workspace = _signed_up(client)

    client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "business_activity": "agency",
            "team_size": "2_5",
            "referral_source": source,
            "referral_source_other": "  Podcast   do João ",
            "client_count": "6_15",
            "organization_name": "Agência Sol",
        },
    )

    organization = Organization.objects.get(pk=workspace.organization_id)
    assert (organization.referral_source, organization.referral_source_other) == (source, kept)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "answers",
    [
        {"action": "skip"},
        {"business_activity": "agency", "team_size": "2_5", "referral_source": "instagram"},
        {
            "business_activity": "agency",
            "team_size": "2_5",
            "referral_source": "instagram",
            "organization_name": "My Organization",
        },
    ],
    ids=["skipped", "no name", "sign-up name"],
)
def test_the_step_cannot_be_skipped_or_left_half_answered(client, answers):
    _user, workspace = _signed_up(client)

    response = client.post(reverse("organizations:about"), {"workspace": str(workspace.id), **answers})

    assert response.status_code == 200
    assert Organization.objects.get(pk=workspace.organization_id).about_answered_at is None
    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id})).headers[
        "Location"
    ] == _about_url(workspace)


@pytest.mark.django_db
def test_an_answer_outside_the_choices_is_refused(client):
    _user, workspace = _signed_up(client)

    response = client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "action": "save",
            "business_activity": "astronaut",
            "organization_name": "Sol",
        },
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


ANSWERS = {"team_size": "2_5", "referral_source": "instagram", "organization_name": "Agência Sol"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("activity", "field", "answer", "other"),
    [
        ("agency", "client_count", "6_15", "business_segment"),
        ("entrepreneur", "business_segment", "food", "client_count"),
    ],
)
def test_the_question_after_the_activity_is_kept_and_the_other_one_is_not(client, activity, field, answer, other):
    _user, workspace = _signed_up(client)

    client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "business_activity": activity,
            field: answer,
            other: "1_5" if other == "client_count" else "food",
            **ANSWERS,
        },
    )

    organization = Organization.objects.get(pk=workspace.organization_id)
    assert (getattr(organization, field), getattr(organization, other)) == (answer, "")
    assert organization.about_answered_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("answer", ["", "9000"], ids=["unanswered", "outside the choices"])
def test_the_question_after_the_activity_must_be_answered_from_its_choices(client, answer):
    _user, workspace = _signed_up(client)

    response = client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "business_activity": "agency", "client_count": answer, **ANSWERS},
    )

    assert response.status_code == 200
    organization = Organization.objects.get(pk=workspace.organization_id)
    assert (organization.about_answered_at, organization.client_count) == (None, "")


@pytest.mark.django_db
def test_a_new_account_can_go_back_and_change_its_answers(client):
    _user, workspace = _signed_up(client)
    client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "business_activity": "agency", "client_count": "1_5", **ANSWERS},
    )

    page = client.get(_about_url(workspace))
    assert page.status_code == 200
    assert page.context["form"]["business_activity"].value() == "agency"

    client.post(
        reverse("organizations:about"),
        {"workspace": str(workspace.id), "business_activity": "entrepreneur", "business_segment": "food", **ANSWERS},
    )
    assert Organization.objects.get(pk=workspace.organization_id).business_activity == "entrepreneur"
    # Named here, the account is still being set up until a brand is in use.
    assert client.get(_about_url(workspace)).status_code == 200
    subscription = billing.ensure_subscription(workspace)
    subscription.status = WorkspaceSubscription.Status.ACTIVE
    subscription.save(update_fields=["status"])
    assert client.get(_about_url(workspace)).headers["Location"] == reverse("dashboard")
