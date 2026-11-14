"""Account centre → Profile: name and postal address of the person contracting."""

import pytest
from django.urls import reverse

from apps.accounts.forms import ProfileForm

ADDRESS = {
    "address_postal_code": "01310100",
    "address_street": "Avenida Paulista",
    "address_number": "1000",
    "address_complement": "Sala 12",
    "address_neighborhood": "Bela Vista",
    "address_city": "São Paulo",
    "address_state": "SP",
}


def _post(client, **data):
    return client.post(reverse("accounts:settings"), {"action": "update_profile", **data})


@pytest.mark.django_db
def test_saving_the_profile_stores_name_and_address(client, user):
    client.force_login(user)

    response = _post(client, first_name=" Ana ", last_name="Souza", **ADDRESS)

    assert response.status_code == 302
    user.refresh_from_db()
    assert (user.first_name, user.last_name) == ("Ana", "Souza")
    assert user.name == "Ana Souza"  # what the rest of the product shows
    assert user.address_postal_code == "01310-100"
    assert user.address_state == "SP"
    assert user.address_complement == "Sala 12"


@pytest.mark.django_db
def test_the_address_may_be_left_blank(client, user):
    client.force_login(user)

    response = _post(client, first_name="Ana", last_name="")

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.name == "Ana"
    assert user.address_street == ""


@pytest.mark.django_db
def test_a_half_filled_address_is_refused_and_nothing_is_saved(client, user):
    client.force_login(user)

    response = _post(client, first_name="Ana", address_street="Avenida Paulista")

    assert response.status_code == 200
    assert "address_city" in response.context["profile_form"].errors
    user.refresh_from_db()
    assert user.address_street == ""
    assert user.name == "Test User"


@pytest.mark.django_db
def test_a_first_name_is_required(client, user):
    client.force_login(user)

    response = _post(client, first_name="  ", last_name="Souza")

    assert response.status_code == 200
    assert "first_name" in response.context["profile_form"].errors
    user.refresh_from_db()
    assert user.name == "Test User"


@pytest.mark.parametrize("cep", ["0131010", "abcdefgh", "013101000"])
@pytest.mark.django_db
def test_a_cep_without_eight_digits_is_refused(user, cep):
    form = ProfileForm({"first_name": "Ana", **ADDRESS, "address_postal_code": cep}, instance=user)

    assert not form.is_valid()
    assert "address_postal_code" in form.errors


@pytest.mark.django_db
def test_an_unknown_state_is_refused(user):
    form = ProfileForm({"first_name": "Ana", **ADDRESS, "address_state": "XX"}, instance=user)

    assert not form.is_valid()
    assert "address_state" in form.errors


@pytest.mark.django_db
def test_an_account_from_before_first_and_last_name_is_offered_its_name_split(user):
    form = ProfileForm(instance=user)  # user.name == "Test User", no first/last yet

    assert form.initial["first_name"] == "Test"
    assert form.initial["last_name"] == "User"
