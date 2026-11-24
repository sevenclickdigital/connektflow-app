from django.db import migrations


def fill_organization(apps, schema_editor):
    """Give the events already logged the organization they were about.

    Through the brand of the subscription or add-on the event was applied
    to; an event of an organization's plan through the gateway subscription
    its payload names; a charge of brands added to a plan through its
    payment. An event that matches none of them stays without one, as an
    event nobody could attribute. Idempotent: only rows without an
    organization are touched.
    """
    BillingEvent = apps.get_model("billing", "BillingEvent")
    OrganizationPlan = apps.get_model("billing", "OrganizationPlan")
    PlanBrandAddition = apps.get_model("billing", "PlanBrandAddition")

    plans = dict(
        OrganizationPlan.objects.exclude(gateway_subscription_id="").values_list(
            "gateway_subscription_id", "organization_id"
        )
    )
    additions = dict(
        PlanBrandAddition.objects.exclude(gateway_payment_id="").values_list(
            "gateway_payment_id", "plan__organization_id"
        )
    )
    events = BillingEvent.objects.filter(organization__isnull=True).select_related(
        "subscription__workspace", "addon__workspace"
    )
    for event in events.iterator(chunk_size=500):
        if event.subscription_id:
            organization_id = event.subscription.workspace.organization_id
        elif event.addon_id:
            organization_id = event.addon.workspace.organization_id
        else:
            payment = event.payload.get("payment") if isinstance(event.payload, dict) else None
            payment = payment if isinstance(payment, dict) else {}
            subscription = payment.get("subscription")
            payment_id = payment.get("id")
            organization_id = (plans.get(subscription) if isinstance(subscription, str) else None) or (
                additions.get(payment_id) if isinstance(payment_id, str) else None
            )
        if organization_id:
            BillingEvent.objects.filter(pk=event.pk).update(organization_id=organization_id)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0012_billingevent_organization_index"),
    ]

    operations = [
        migrations.RunPython(fill_organization, reverse_code=migrations.RunPython.noop),
    ]
