"""Every Asaas customer Connekt Flow creates joins ASAAS_CUSTOMER_GROUP, and
the command puts the ones created before into it."""

from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

UPDATE = "apps.billing.gateways.asaas.AsaasClient.update_customer"


@override_settings(ASAAS_CUSTOMER_GROUP="Connekt Flow")
def test_a_new_customer_is_created_in_the_group():
    org = Organization.objects.create(name="Acme")
    client = mock.Mock()
    client.find_customer_by_reference.return_value = None
    client.create_customer.return_value = {"id": "cus_1"}

    contracting.ensure_customer(org, client=client, cpf_cnpj="52998224725")

    assert client.create_customer.call_args.kwargs["group_name"] == "Connekt Flow"


def _customer(name, customer_id):
    org = Organization.objects.create(name=name)
    profile = services.ensure_billing_profile(org)
    profile.gateway, profile.gateway_customer_id = "asaas", customer_id
    profile.save(update_fields=["gateway", "gateway_customer_id"])
    return org


@override_settings(ASAAS_CUSTOMER_GROUP="Connekt Flow")
def test_the_command_groups_the_existing_customers_and_reports_refusals():
    _customer("Acme", "cus_1")
    _customer("Beta", "cus_2")
    Organization.objects.create(name="Never contracted")

    with mock.patch(UPDATE) as update:
        call_command("asaas_group_customers", stdout=StringIO())
    assert sorted(c.args[0] for c in update.call_args_list) == ["cus_1", "cus_2"]
    assert {c.kwargs["group_name"] for c in update.call_args_list} == {"Connekt Flow"}

    with mock.patch(UPDATE, side_effect=[AsaasAPIError("no", status_code=400), None]), pytest.raises(CommandError):
        call_command("asaas_group_customers", stdout=StringIO(), stderr=StringIO())


def test_a_dry_run_changes_nothing():
    _customer("Acme", "cus_1")

    with mock.patch(UPDATE) as update:
        call_command("asaas_group_customers", "--dry-run", stdout=StringIO())

    update.assert_not_called()
