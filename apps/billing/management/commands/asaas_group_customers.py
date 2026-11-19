"""Put the organizations' existing Asaas customers into the customer group.

Customers created from now on join ``ASAAS_CUSTOMER_GROUP`` when they are
created (``contracting.ensure_customer``). This catches up the ones created
before, so every Connekt Flow payer sits in the same group as the other tools'
customers do in theirs.

    python manage.py asaas_group_customers --dry-run
    python manage.py asaas_group_customers

Only customers this app created are touched: the ids stored on
``OrganizationBillingProfile``. A customer Asaas refuses is reported and the
rest carry on; the command then exits with an error.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.billing.gateways.asaas import GATEWAY, AsaasClient, AsaasError
from apps.billing.models import OrganizationBillingProfile


class Command(BaseCommand):
    help = "Put the existing Asaas customers of every organization into ASAAS_CUSTOMER_GROUP."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List the customers without changing them.")

    def handle(self, *args, dry_run=False, **options):
        group = settings.ASAAS_CUSTOMER_GROUP
        if not group:
            raise CommandError("ASAAS_CUSTOMER_GROUP is empty: there is no group to put the customers in.")

        profiles = (
            OrganizationBillingProfile.objects.filter(gateway=GATEWAY)
            .exclude(gateway_customer_id="")
            .select_related("organization")
            .order_by("organization__name")
        )
        client = AsaasClient()
        failed = 0
        for profile in profiles:
            label = f"{profile.gateway_customer_id} ({profile.organization.name})"
            if dry_run:
                self.stdout.write(f"would group {label}")
                continue
            try:
                client.update_customer(profile.gateway_customer_id, group_name=group)
            except AsaasError as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"failed {label}: {exc}"))
            else:
                self.stdout.write(f"grouped {label}")

        if failed:
            raise CommandError(f"{failed} customer(s) could not be put in {group!r}.")
        verb = "Would put" if dry_run else "Put"
        self.stdout.write(self.style.SUCCESS(f"{verb} {profiles.count()} customer(s) in {group!r}."))
