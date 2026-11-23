"""One person, several organizations: creating them, moving between them by
opening a brand, and what stays separate (team, payer, trial, deletion)."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services as billing
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _signed_up(client, email="dona@mac.example"):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    client.force_login(user)
    return user, WorkspaceMembership.objects.get(user=user).workspace


def _create_org(client, organization_name="Bob's", brand_name="Bob's Centro"):
    response = client.post(
        reverse("organizations:create"),
        {
            "organization_name": organization_name,
            "brand_name": brand_name,
            "tax_id": "11.444.777/0001-61",
            "phone": "(21) 98888-1111",
        },
    )
    return response, Workspace.objects.filter(name=brand_name).first()


@pytest.mark.django_db
def test_a_second_organization_is_created_with_its_first_brand(client):
    user, first = _signed_up(client)

    response, brand = _create_org(client)

    assert response.headers["Location"] == reverse("billing:subscribe_modal", kwargs={"workspace_id": brand.id})
    assert brand.organization.name == "Bob's"
    assert brand.organization_id != first.organization_id
    assert OrgMembership.objects.get(user=user, organization=brand.organization).org_role == "owner"
    assert WorkspaceMembership.objects.get(user=user, workspace=brand).workspace_role == "owner"


@pytest.mark.django_db
def test_the_account_centre_follows_the_brand_that_is_open(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    def in_use():
        groups = client.get(reverse("organizations:workspaces")).context["org_groups"]
        return [g["organization"] for g in groups if g["is_current"]]

    client.get(reverse("calendar:calendar", kwargs={"workspace_id": first.id}))
    assert in_use() == [first.organization]

    client.get(reverse("calendar:calendar", kwargs={"workspace_id": second.id}))
    assert in_use() == [second.organization]


@pytest.mark.django_db
def test_the_switcher_lists_the_brands_under_each_organization(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    groups = client.get(reverse("organizations:workspaces")).context["sidebar_org_groups"]

    assert [(g["organization"].id, [w.id for w in g["workspaces"]]) for g in groups] == [
        (first.organization_id, [first.id]),
        (second.organization_id, [second.id]),
    ]
    assert all(g["can_create"] for g in groups)


@pytest.mark.django_db
def test_a_brand_is_created_in_the_organization_that_was_chosen(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)  # the second organization is now the one in use

    client.post(reverse("workspaces:create"), {"name": "Mac Shopping", "organization": str(first.organization_id)})

    assert Workspace.objects.get(name="Mac Shopping").organization_id == first.organization_id


@pytest.mark.django_db
def test_a_brand_cannot_be_created_in_someone_elses_organization(client):
    _signed_up(client)
    foreign = Organization.objects.create(name="Foreign")

    response = client.post(reverse("workspaces:create"), {"name": "Intruder", "organization": str(foreign.id)})

    assert response.status_code == 403
    assert not Workspace.objects.filter(name="Intruder").exists()


@pytest.mark.django_db
def test_a_plain_member_cannot_create_a_brand_there(client):
    user, _own = _signed_up(client)
    other = Organization.objects.create(name="Agency")
    OrgMembership.objects.create(user=user, organization=other, org_role=OrgMembership.OrgRole.MEMBER)

    response = client.post(reverse("workspaces:create"), {"name": "Nope", "organization": str(other.id)})

    assert response.status_code == 403


@pytest.mark.django_db
def test_each_organization_has_its_own_payer(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    client.post(
        reverse("billing_account:update_payer"),
        {"payer_name": "Bob's Ltda", "tax_id": "11.222.333/0001-81", "phone": "", "billing_email": ""},
    )

    assert billing.ensure_billing_profile(second.organization).tax_id == "11222333000181"
    assert billing.ensure_billing_profile(first.organization).tax_id == ""


@pytest.mark.django_db
def test_the_free_trial_is_one_per_person_not_one_per_organization(client, settings):
    settings.BILLING_TRIAL_DAYS = 7
    user, first = _signed_up(client)
    billing.start_trial(first, tax_id="52998224725", phone="11988887777", email=user.email, user=user)
    _response, second = _create_org(client)

    assert billing.trial_available(second, user) is False
    with pytest.raises(billing.TrialRefusedError):
        billing.start_trial(second, tax_id="11144477735", phone="11977776666", email="", user=user)


@pytest.mark.django_db
def test_deleting_one_of_two_organizations_keeps_the_account(client):
    user, first = _signed_up(client)
    _response, second = _create_org(client)  # in use

    response = client.post(reverse("organizations:settings"), {"action": "delete_organization_now"})

    assert response.headers["Location"] == reverse("dashboard")
    assert User.objects.filter(pk=user.pk).exists()
    assert not Organization.objects.filter(pk=second.organization_id).exists()
    assert Organization.objects.filter(pk=first.organization_id).exists()
    # Still signed in, and what is left opens.
    assert client.get(reverse("dashboard"), follow=True).status_code == 200


@pytest.mark.django_db
def test_deleting_the_only_organization_still_closes_the_account(client):
    user, _first = _signed_up(client)

    client.post(reverse("organizations:settings"), {"action": "delete_organization_now"})

    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_an_organization_needs_both_names(client):
    _signed_up(client)
    before = Organization.objects.count()

    _create_org(client, organization_name="  ", brand_name="Brand")

    assert Organization.objects.count() == before


@pytest.mark.django_db
def test_the_billing_page_shows_the_brands_of_the_organization_in_use_only(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)  # in use

    names = {row["workspace"].name for row in client.get(reverse("billing_account:overview")).context["rows"]}
    assert names == {second.name}

    client.get(reverse("calendar:calendar", kwargs={"workspace_id": first.id}))
    names = {row["workspace"].name for row in client.get(reverse("billing_account:overview")).context["rows"]}
    assert names == {first.name}


@pytest.mark.django_db
def test_a_new_account_is_named_before_the_plan_and_names_its_brand_and_payer_on_the_checkout(client, settings):
    settings.BILLING_TRIAL_DAYS = 7
    _user, workspace = _signed_up(client)
    checkout = reverse("billing:checkout", kwargs={"workspace_id": workspace.id})
    client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "business_activity": "agency",
            "team_size": "solo",
            "referral_source": "google",
            "organization_name": "Agência Sol",
        },
    )
    assert Organization.objects.get(pk=workspace.organization_id).name == "Agência Sol"

    landing = client.get(reverse("dashboard"), follow=True)
    assert landing.redirect_chain[-1][0] == checkout
    # Still in setup: the checkout shows the company step as done, to go back to.
    assert landing.context["in_setup"] is True

    response = client.post(
        reverse("billing:start_trial", kwargs={"workspace_id": workspace.id}),
        {
            f"name_{workspace.id}": "Padaria Sol",
            "payer_name": "Sol Comunicação Ltda",
            "tax_id": "529.982.247-25",
            "phone": "(11) 98888-7777",
        },
    )

    assert response.headers["Location"].startswith(
        reverse("billing:checkout_welcome", kwargs={"workspace_id": workspace.id})
    )
    workspace.refresh_from_db()
    assert (workspace.organization.name, workspace.name) == ("Agência Sol", "Padaria Sol")
    profile = billing.ensure_billing_profile(workspace.organization)
    assert (profile.payer_name, profile.tax_id, profile.phone) == (
        "Sol Comunicação Ltda",
        "52998224725",
        "11988887777",
    )


@pytest.mark.django_db
def test_the_welcome_step_keeps_asking_until_both_names_are_given(client):
    _user, workspace = _signed_up(client)

    client.post(
        reverse("organizations:welcome"),
        {"workspace": str(workspace.id), "organization_name": "", "brand_name": "Padaria Sol"},
    )

    workspace.refresh_from_db()
    assert workspace.organization.is_unnamed
    assert workspace.name != "Padaria Sol"


@pytest.mark.django_db
def test_nobody_names_someone_elses_organization(client):
    _owner, workspace = _signed_up(client)
    intruder = User.objects.create_user(email="x@y.example", password="x", tos_accepted_at=timezone.now())
    client.force_login(intruder)

    client.post(
        reverse("organizations:welcome"),
        {"workspace": str(workspace.id), "organization_name": "Hijacked", "brand_name": "Hijacked"},
    )

    workspace.refresh_from_db()
    assert workspace.organization.is_unnamed


@pytest.mark.django_db
def test_an_account_already_in_use_is_not_sent_back_to_the_welcome_step(client):
    _user, workspace = _signed_up(client)
    billing.grant_courtesy(workspace)

    landing = client.get(reverse("dashboard"), follow=True)

    assert landing.redirect_chain[-1][0] == reverse("workspaces:dashboard", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
def test_the_welcome_step_refuses_an_invalid_document(client):
    _user, workspace = _signed_up(client)

    client.post(
        reverse("organizations:welcome"),
        {
            "workspace": str(workspace.id),
            "organization_name": "Agência Sol",
            "brand_name": "Padaria Sol",
            "tax_id": "111.111.111-11",
            "phone": "(11) 98888-7777",
        },
    )

    workspace.refresh_from_db()
    assert workspace.organization.is_unnamed


@pytest.mark.django_db
def test_the_trial_starts_with_the_data_given_at_the_welcome_step(client, settings):
    settings.BILLING_TRIAL_DAYS = 7
    _user, workspace = _signed_up(client)
    client.post(
        reverse("organizations:welcome"),
        {
            "workspace": str(workspace.id),
            "organization_name": "Agência Sol",
            "brand_name": "Padaria Sol",
            "tax_id": "529.982.247-25",
            "phone": "(11) 98888-7777",
        },
    )

    client.post(reverse("billing:start_trial", kwargs={"workspace_id": workspace.id}))

    profile = billing.ensure_billing_profile(workspace.organization)
    assert profile.trial_started_at is not None
    assert profile.trial_tax_id == "52998224725"


@pytest.mark.django_db
def test_the_organizations_page_lists_each_one_with_its_brands(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    groups = client.get(reverse("organizations:workspaces")).context["org_groups"]

    # Newest first.
    assert [(g["organization"].id, [e["workspace"].id for e in g["brands"]]) for g in groups] == [
        (second.organization_id, [second.id]),
        (first.organization_id, [first.id]),
    ]
    assert all(g["can_edit"] for g in groups)


@pytest.mark.django_db
def test_an_organization_that_is_not_in_use_is_renamed_from_the_page(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    client.post(reverse("organizations:rename"), {"organization": first.organization_id, "name": "Grupo Mac"})

    first.organization.refresh_from_db()
    second.organization.refresh_from_db()
    assert first.organization.name == "Grupo Mac"
    assert second.organization.name == "Bob's"


@pytest.mark.django_db
def test_an_organization_of_someone_else_is_not_renamed(client):
    _signed_up(client)
    foreign = Organization.objects.create(name="Alheia")
    guest_of = Organization.objects.create(name="Só membro")
    OrgMembership.objects.create(
        user=User.objects.get(email="dona@mac.example"), organization=guest_of, org_role="member"
    )

    for organization in (foreign, guest_of):
        response = client.post(reverse("organizations:rename"), {"organization": organization.id, "name": "Tomada"})
        assert response.status_code == 403
        organization.refresh_from_db()
        assert organization.name != "Tomada"


@pytest.mark.django_db
def test_an_invitation_goes_to_the_organization_that_was_chosen(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)
    # The organization in use is the second; the invitation names the first.
    client.get(reverse("calendar:calendar", kwargs={"workspace_id": second.id}))

    response = client.post(
        reverse("members:invite"),
        {
            "organization": first.organization_id,
            "email": "nova@mac.example",
            "org_role": "member",
            f"ws_{first.id}": "1",
            f"ws_role_{first.id}": "editor",
            # A brand of the other organization is not assigned through this one.
            f"ws_{second.id}": "1",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 204
    invitation = Invitation.objects.get(email="nova@mac.example")
    assert invitation.organization_id == first.organization_id
    assert [a["workspace_id"] for a in invitation.workspace_assignments] == [str(first.id)]


@pytest.mark.django_db
def test_nobody_is_invited_into_an_organization_the_user_does_not_administer(client):
    user, _first = _signed_up(client)
    foreign = Organization.objects.create(name="Alheia")
    member_of = Organization.objects.create(name="Só membro")
    OrgMembership.objects.create(user=user, organization=member_of, org_role="member")

    for organization in (foreign, member_of):
        response = client.post(reverse("members:invite"), {"organization": organization.id, "email": "x@mac.example"})
        assert response.status_code == 403
    assert not Invitation.objects.filter(email="x@mac.example").exists()


@pytest.mark.django_db
def test_the_invitation_form_offers_each_organization_with_its_brands(client):
    _user, first = _signed_up(client)
    _response, second = _create_org(client)

    groups = client.get(reverse("members:list")).context["invite_orgs"]

    assert [(g["organization"].id, [w.id for w in g["workspaces"]]) for g in groups] == [
        (second.organization_id, [second.id]),
        (first.organization_id, [first.id]),
    ]


@pytest.mark.django_db
def test_the_organizations_page_counts_only_what_is_the_users(client):
    user, first = _signed_up(client)
    _response, second = _create_org(client)
    colleague = User.objects.create_user(email="colega@mac.example", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=colleague, organization=first.organization, org_role="member")
    # The colleague's own organization is nobody else's business.
    foreign = OrgMembership.objects.filter(user=colleague).exclude(organization=first.organization).get()

    context = client.get(reverse("organizations:workspaces")).context

    by_org = {g["organization"].id: g for g in context["org_groups"]}
    assert foreign.organization_id not in by_org
    assert by_org[first.organization_id]["member_count"] == 2
    assert by_org[second.organization_id]["member_count"] == 1
    assert by_org[first.organization_id]["brand_count"] == 1
    # One person in two organizations is counted once.
    assert context["totals"] == {"organizations": 2, "members": 2, "brands": 2, "channels": 0}
