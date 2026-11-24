"""The CEP lookup behind the profile's address form.

ViaCEP is the external boundary and the only thing mocked.
"""

from __future__ import annotations

from unittest import mock

import httpx
import pytest
from django.urls import reverse

from apps.accounts import address
from apps.accounts.forms import ProfileForm

URL = reverse("accounts:cep_lookup")


def _viacep(payload, status=200):
    return mock.patch(
        "apps.accounts.address.httpx.get",
        return_value=httpx.Response(status, json=payload, request=httpx.Request("GET", "https://viacep.com.br")),
    )


@pytest.mark.django_db
class TestCepLookup:
    def test_a_visitor_is_sent_to_log_in(self, client):
        response = client.get(URL, {"cep": "01310-100"})

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_the_address_comes_back_under_the_form_s_field_names(self, client, user):
        client.force_login(user)
        payload = {"logradouro": "Avenida Paulista", "bairro": "Bela Vista", "localidade": "São Paulo", "uf": "SP"}

        with _viacep(payload) as get:
            response = client.get(URL, {"cep": "01310-100"})

        assert response.status_code == 200
        assert response.json() == {
            "street": "Avenida Paulista",
            "neighborhood": "Bela Vista",
            "city": "São Paulo",
            "state": "SP",
        }
        assert get.call_args.args[0].endswith("/01310100/json/")

    @pytest.mark.parametrize("cep", ["", "123", "abcdefgh", "0131010099"])
    def test_what_is_not_a_cep_is_refused_without_calling_out(self, client, user, cep):
        client.force_login(user)

        with _viacep({}) as get:
            response = client.get(URL, {"cep": cep})

        assert response.status_code == 400
        get.assert_not_called()

    def test_an_unknown_cep_is_a_404(self, client, user):
        client.force_login(user)

        with _viacep({"erro": "true"}):
            response = client.get(URL, {"cep": "99999999"})

        assert response.status_code == 404

    def test_a_failing_service_is_a_502_not_a_crash(self, client, user):
        client.force_login(user)

        with mock.patch("apps.accounts.address.httpx.get", side_effect=httpx.ConnectError("down")):
            response = client.get(URL, {"cep": "01310100"})

        assert response.status_code == 502

    def test_an_error_status_from_the_service_is_a_502(self, client, user):
        client.force_login(user)

        with _viacep({}, status=500):
            response = client.get(URL, {"cep": "01310100"})

        assert response.status_code == 502

    def test_a_town_wide_cep_comes_with_empty_street(self):
        with _viacep({"logradouro": "", "bairro": "", "localidade": "Pomerode", "uf": "SC"}):
            found = address.lookup_cep("89107000")

        assert found == {"street": "", "neighborhood": "", "city": "Pomerode", "state": "SC"}


def test_the_cep_field_is_masked():
    assert ProfileForm().fields["address_postal_code"].widget.attrs["data-mask"] == "cep"


@pytest.mark.django_db
class TestAddressFromTheCheckout:
    """The CEP and number typed for the card become the address of a profile that has none."""

    def test_an_empty_profile_keeps_them_and_a_task_fills_the_rest(self, user):
        from background_task.models import Task

        assert address.remember_address(user, "88115544", " 1189 ") is True

        user.refresh_from_db()
        assert (user.address_postal_code, user.address_number, user.address_city) == ("88115-544", "1189", "")
        assert Task.objects.filter(task_name="apps.accounts.tasks.complete_address_from_cep").count() == 1

        with _viacep({"logradouro": "Rua A", "bairro": "Centro", "localidade": "São José", "uf": "SC"}):
            assert address.complete_address(user) is True
            assert address.complete_address(user) is False

        user.refresh_from_db()
        assert (user.address_street, user.address_neighborhood) == ("Rua A", "Centro")
        assert (user.address_city, user.address_state, user.address_number) == ("São José", "SC", "1189")

    def test_an_address_the_person_wrote_is_never_replaced(self, user):
        user.address_postal_code = "01310-100"
        user.address_number = "10"
        user.save()

        assert address.remember_address(user, "88115544", "1189") is False

        user.refresh_from_db()
        assert (user.address_postal_code, user.address_number) == ("01310-100", "10")

    @pytest.mark.parametrize(("cep", "number"), [("123", "10"), ("88115544", "")])
    def test_something_that_is_not_an_address_is_not_kept(self, user, cep, number):
        assert address.remember_address(user, cep, number) is False
