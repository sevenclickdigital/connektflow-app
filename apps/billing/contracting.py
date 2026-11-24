"""Contracting: turning a local subscription row into a live Asaas one.

Sits between ``services.py`` (pure local arithmetic, no network) and
``gateways/asaas.py`` (pure HTTP, no database). The dependency runs one
way only — ``services`` never imports this module — so the money rules
stay testable without a gateway.

The split of roles across the two systems is the thing to keep in mind:

    the PAYER is the Organization  → OrganizationBillingProfile
                                     .gateway_customer_id  (cus_…)
    the SUBSCRIPTION is per Workspace → WorkspaceSubscription
                                     .gateway_subscription_id  (sub_…)

Asaas keys payment methods to the customer, so an organization with five
workspaces is one customer and five subscriptions. Entering card details
once then covers every workspace the org adds later.

Everything here is idempotent, because the modal in the UI is one click
away from being double-submitted and the recovery path after a timeout
is a retry. Idempotency is held in three layers:

1. The local row is checked first. A workspace that already has a
   ``gateway_subscription_id`` is returned untouched — no HTTP at all.
2. Before creating, Asaas is asked whether an object already exists for
   our ``externalReference``. This is what covers the nasty case: the
   POST succeeded, the response never arrived, and we have no id.
3. ``externalReference`` is the local primary key (org id for the
   customer, workspace id for the subscription), so step 2 can only ever
   match the object we would have created.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.affiliates import services as affiliate_services
from apps.billing import emails, services
from apps.billing.gateways import asaas as asaas_gateway
from apps.billing.gateways.asaas import GATEWAY, AsaasClient, AsaasError
from apps.billing.models import (
    BillingEvent,
    OrganizationPlan,
    PlanBrandAddition,
    WorkspaceAddon,
    WorkspaceSubscription,
)

logger = logging.getLogger(__name__)

#: Asaas statuses of a subscription that will not charge again.
ENDED_SUBSCRIPTION_STATUSES = frozenset({"INACTIVE", "EXPIRED"})

#: Pix Automático authorization statuses: paid and authorized, and over.
PIX_AUTHORIZATION_ACTIVE = "ACTIVE"
PIX_AUTHORIZATION_ENDED = frozenset({"CANCELLED", "REFUSED", "EXPIRED"})

#: Asaas statuses of a charge the customer still has to pay.
UNPAID_PAYMENT_STATUSES = frozenset({"PENDING", "OVERDUE"})


def has_customer(organization):
    """Whether the organization already exists as a customer in Asaas.

    What the screens ask to decide if the CPF/CNPJ field is shown: Asaas
    wants the document once, when the customer is created.
    """
    profile = services.ensure_billing_profile(organization)
    return bool(profile.gateway_customer_id and profile.gateway == GATEWAY)


def ensure_customer(organization, *, client=None, email="", cpf_cnpj=""):
    """Make sure the organization exists as a customer in Asaas.

    Returns the ``OrganizationBillingProfile`` carrying the id. Safe to
    call repeatedly: the stored id short-circuits, and a stored id that
    predates a gateway switch is replaced rather than reused.

    Asaas refuses to create a customer without a CPF or CNPJ. It comes
    from ``cpf_cnpj`` or, when the account already gave it (to start a
    free trial), from the billing profile, where a new one is kept.
    """
    client = client or AsaasClient()
    profile = services.ensure_billing_profile(organization)

    if profile.gateway_customer_id and profile.gateway == GATEWAY:
        return profile

    cpf_cnpj = cpf_cnpj or profile.tax_id

    # Asaas may already hold a customer for this org from a call whose
    # response we lost. Adopting it beats creating a duplicate, which
    # would strand the org's saved card on the orphan.
    existing = client.find_customer_by_reference(organization.pk)
    if existing is None:
        existing = client.create_customer(
            name=profile.payer_name or organization.name,
            external_reference=organization.pk,
            # The billing email the account chose comes before the email of
            # whoever happened to press Contract.
            email=organization.billing_email or email,
            cpf_cnpj=cpf_cnpj,
            mobile_phone=profile.phone,
            group_name=settings.ASAAS_CUSTOMER_GROUP,
        )

    customer_id = existing.get("id") or ""
    if not customer_id:
        raise AsaasError("Asaas returned a customer without an id.")

    profile.gateway = GATEWAY
    profile.gateway_customer_id = customer_id
    profile.tax_id = cpf_cnpj
    profile.save(update_fields=["gateway", "gateway_customer_id", "tax_id", "updated_at"])
    return profile


def update_payer(organization, *, name, tax_id, phone, email, client=None):
    """Change who the organization's invoices are issued to.

    An empty ``tax_id``, ``phone`` or ``email`` keeps what is stored. When the organization
    already is a customer at Asaas the change is sent there first, and a
    refusal (``AsaasError``) leaves the local data as it was: the two
    must not disagree about who is being invoiced.
    """
    profile = services.ensure_billing_profile(organization)
    tax_id = tax_id or profile.tax_id
    phone = phone or profile.phone
    email = email or organization.billing_email

    if profile.gateway_customer_id and profile.gateway == GATEWAY:
        (client or AsaasClient()).update_customer(
            profile.gateway_customer_id,
            name=name,
            email=email,
            cpf_cnpj=tax_id,
            mobile_phone=phone,
            group_name=settings.ASAAS_CUSTOMER_GROUP,
        )

    with transaction.atomic():
        profile.payer_name = name
        profile.tax_id = tax_id
        profile.phone = phone
        profile.save(update_fields=["payer_name", "tax_id", "phone", "updated_at"])
        organization.billing_email = email
        organization.save(update_fields=["billing_email"])
    return profile


def subscribe_workspace(workspace, *, client=None, email="", cpf_cnpj="", cycle=WorkspaceSubscription.Cycle.MONTHLY):
    """Contract the monthly subscription for one workspace.

    The call the contracting modal makes. Ensures the org's customer,
    creates the recurring subscription, and records both ids on the
    workspace's ``WorkspaceSubscription``.

    This is the moment the price is fixed: the row takes today's quote
    for the chosen ``cycle`` (list price, volume and yearly discounts) and
    keeps it from then on.

    The row stays ``pending``: a created subscription is an agreement to
    pay, not a payment. Only ``PAYMENT_CONFIRMED`` on the webhook flips
    it to ``active``, so an unpaid boleto never grants access.

    Returns the ``WorkspaceSubscription``. Raises ``AsaasError`` on any
    gateway failure, leaving the local row untouched for a clean retry.
    """
    client = client or AsaasClient()
    sub = services.ensure_subscription(workspace)

    # Layer 1: already contracted. Returned as-is rather than re-POSTed,
    # which is what makes a double-clicked modal harmless. A canceled row
    # is the exception: its gateway subscription is gone, so contracting
    # again has to create a new one.
    was_canceled = sub.status == WorkspaceSubscription.Status.CANCELED
    if sub.gateway_subscription_id and not was_canceled:
        return sub

    # A comped workspace has no gateway subscription behind it by
    # design; charging one would be a billing incident.
    if sub.is_courtesy:
        return sub

    profile = ensure_customer(workspace.organization, client=client, email=email, cpf_cnpj=cpf_cnpj)

    # Layer 2: recover an id we may have failed to persist. One that Asaas
    # already ended is not recovered, or a returning customer would be
    # tied to a subscription that never charges again.
    remote = client.find_subscription_by_reference(workspace.pk)
    if remote is not None and (remote.get("deleted") or remote.get("status") in ENDED_SUBSCRIPTION_STATUSES):
        remote = None
    if remote is not None and remote.get("cycle") == asaas_gateway.CYCLE_YEARLY:
        # Adopted from a call whose answer was lost: what Asaas holds decides.
        cycle = WorkspaceSubscription.Cycle.YEARLY
    quote = services.quote_workspace(workspace, cycle)
    if remote is None:
        remote = client.create_subscription(
            customer_id=profile.gateway_customer_id,
            value_cents=quote["charge_cents"],
            cycle=(
                asaas_gateway.CYCLE_YEARLY
                if quote["cycle"] == WorkspaceSubscription.Cycle.YEARLY
                else asaas_gateway.CYCLE_MONTHLY
            ),
            external_reference=workspace.pk,
            # Translated: it reaches the customer, on the Asaas invoice
            # and in the charge notification emails Asaas sends.
            description=str(_("Connekt Flow — workspace %(name)s") % {"name": workspace.name}),
        )

    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")

    with transaction.atomic():
        sub.gateway = GATEWAY
        sub.gateway_customer_id = profile.gateway_customer_id
        sub.gateway_subscription_id = subscription_id
        sub.canceled_at = None
        sub.price_cents = quote["monthly_cents"]
        sub.billing_cycle = quote["cycle"]
        if was_canceled:
            sub.status = WorkspaceSubscription.Status.PENDING
        sub.save(
            update_fields=[
                "price_cents",
                "billing_cycle",
                "gateway",
                "gateway_customer_id",
                "gateway_subscription_id",
                "canceled_at",
                "status",
                "updated_at",
            ]
        )
    logger.info("Contracted Asaas subscription for workspace %s", workspace.pk)
    return sub


def open_invoice_url(workspace, *, client=None):
    """Where the customer pays what this workspace owes, or "".

    The invoice page of the oldest unpaid charge among the workspace's
    subscription and its storage add-ons, each of which is its own
    subscription at Asaas. Asked from Asaas each time rather than stored:
    the charges change every cycle and a paid one must stop being offered.
    """
    sub = services.ensure_subscription(workspace)
    subscription_ids = []
    if sub.gateway_subscription_id and sub.status != WorkspaceSubscription.Status.CANCELED:
        subscription_ids.append(sub.gateway_subscription_id)
    subscription_ids.extend(
        WorkspaceAddon.objects.filter(workspace=workspace)
        .exclude(status=WorkspaceAddon.Status.CANCELED)
        .exclude(gateway_subscription_id="")
        .values_list("gateway_subscription_id", flat=True)
    )
    if not subscription_ids:
        return ""
    client = client or AsaasClient()
    unpaid = [
        payment
        for subscription_id in subscription_ids
        for payment in client.list_subscription_payments(subscription_id)
        if payment.get("status") in UNPAID_PAYMENT_STATUSES and payment.get("invoiceUrl")
    ]
    if not unpaid:
        return ""
    unpaid.sort(key=lambda payment: payment.get("dueDate") or "")
    url = unpaid[0]["invoiceUrl"]
    # The customer is redirected there, so it has to be a web address.
    return url if url.startswith("https://") else ""


def subscribe_storage_addon(addon, *, client=None, cpf_cnpj=""):
    """Contract an extra-storage add-on as its own Asaas subscription.

    Asaas has no concept of a line item on an existing subscription, so
    an add-on is a second recurring charge against the same customer —
    which is why ``WorkspaceAddon`` carries its own
    ``gateway_subscription_id``.

    The add-on stays ``pending`` and therefore grants no disk until its
    first payment is confirmed.
    """
    client = client or AsaasClient()
    if addon.gateway_subscription_id:
        return addon

    workspace = addon.workspace
    profile = ensure_customer(workspace.organization, client=client, cpf_cnpj=cpf_cnpj)

    # Namespaced so the add-on's reference can never collide with the
    # workspace subscription's, which is the bare workspace id.
    reference = f"addon:{addon.pk}"
    remote = client.find_subscription_by_reference(reference)
    if remote is None:
        remote = client.create_subscription(
            customer_id=profile.gateway_customer_id,
            value_cents=addon.total_price_cents,
            external_reference=reference,
            description=str(
                _("Connekt Flow — %(gb)s GB extra storage for %(name)s")
                % {"gb": addon.quantity, "name": workspace.name}
            ),
        )

    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")

    addon.gateway = GATEWAY
    addon.gateway_subscription_id = subscription_id
    addon.save(update_fields=["gateway", "gateway_subscription_id", "updated_at"])
    return addon


def cancel_workspace_subscription(workspace, *, client=None):
    """Cancel a workspace's subscription at the gateway and locally.

    The local row is marked ``canceled`` even when it had no gateway
    subscription (a pending workspace that was never paid for), so the
    customer's intent is recorded either way.

    A gateway 404 is treated as success: the subscription being absent
    upstream is the state we were asking for, and raising would leave
    the local row stuck active against a subscription that is already
    gone.
    """
    client = client or AsaasClient()
    sub = services.ensure_subscription(workspace)

    if sub.gateway_subscription_id:
        try:
            client.cancel_subscription(sub.gateway_subscription_id)
        except asaas_gateway.AsaasAPIError as exc:
            if exc.status_code != 404:
                raise

    sub.status = WorkspaceSubscription.Status.CANCELED
    sub.canceled_at = sub.canceled_at or timezone.now()
    sub.save(update_fields=["status", "canceled_at", "updated_at"])
    return sub


def cancel_organization_subscriptions(organization, *, client=None):
    """Stop every gateway charge of ``organization`` before it is deleted.

    Its workspaces' subscriptions, add-ons and plan are cancelled at Asaas; the rows
    themselves go with the organization's cascade. Called by
    ``Organization.hard_delete``, so no deletion path leaves a customer being
    charged for an account that no longer exists. A gateway 404 counts as done,
    as in ``cancel_workspace_subscription``; any other error is raised, and the
    caller must not delete the organization, since its charges would carry on.
    Returns how many subscriptions were cancelled.
    """
    gateway_ids = [
        *WorkspaceSubscription.objects.filter(workspace__organization=organization)
        .exclude(gateway_subscription_id="")
        .values_list("gateway_subscription_id", flat=True),
        *WorkspaceAddon.objects.filter(workspace__organization=organization)
        .exclude(gateway_subscription_id="")
        .values_list("gateway_subscription_id", flat=True),
        *OrganizationPlan.objects.filter(organization=organization)
        .exclude(gateway_subscription_id="")
        .values_list("gateway_subscription_id", flat=True),
    ]
    if not gateway_ids:
        return 0
    client = client or AsaasClient()
    for subscription_id in gateway_ids:
        try:
            client.cancel_subscription(subscription_id)
        except asaas_gateway.AsaasAPIError as exc:
            if exc.status_code != 404:
                raise
    logger.info("Cancelled %d gateway subscriptions of organization %s", len(gateway_ids), organization.pk)
    return len(gateway_ids)


# ---------------------------------------------------------------------------
# Organization plan — brands bought together, paid by card
# ---------------------------------------------------------------------------

#: Asaas statuses of a charge that has been paid.
PAID_PAYMENT_STATUSES = frozenset({"CONFIRMED", "RECEIVED"})


class CardRefusedError(Exception):
    """The card was not charged. The message is for the customer."""


class CardChallengeRequiredError(Exception):
    """The issuer wants the payer to authenticate before the charge.

    ``url`` is the issuer's page; the plan activates when the gateway
    confirms the payment afterwards (the webhook).
    """

    def __init__(self, url):
        super().__init__(url)
        self.url = url


def _plan_description(organization, brands):
    # Translated: it reaches the customer on the Asaas charge and its emails.
    return str(
        _("Connekt Flow — %(brands)s brands of %(name)s") % {"brands": brands, "name": organization.name}
        if brands != 1
        else _("Connekt Flow — 1 brand of %(name)s") % {"name": organization.name}
    )


def _first_unpaid(client, subscription_id):
    unpaid = [
        p for p in client.list_subscription_payments(subscription_id) if p.get("status") in UNPAID_PAYMENT_STATUSES
    ]
    unpaid.sort(key=lambda payment: payment.get("dueDate") or "")
    return unpaid[0] if unpaid else None


def _activate_plan(plan, *, card=None, coupon_saving_cents=0, workspaces=()):
    """Mark a paid plan active, record its coupon as used, and open the workspaces it covers.

    ``workspaces`` are the brands chosen at checkout, covered before any other.
    """
    newly_active = not plan.is_active
    plan.status = WorkspaceSubscription.Status.ACTIVE
    plan.canceled_at = None
    fields = ["status", "canceled_at", "updated_at"]
    if card:
        plan.card_brand = str(card.get("creditCardBrand") or "")[:32]
        plan.card_last4 = str(card.get("creditCardNumber") or "")[-4:]
        fields += ["card_brand", "card_last4"]
    plan.save(update_fields=fields)
    if plan.coupon_id:
        services.redeem_coupon(plan.coupon, plan.organization, discount_cents=coupon_saving_cents)
    services.sync_plan_coverage(plan, workspaces)
    if newly_active:
        emails.send_payment_confirmed(plan.organization, plan=plan)
    return plan


def _settle_earlier_attempt(plan, client):
    """Deal with what a previous checkout left behind at the gateway.

    A Pix QR code generated earlier is withdrawn, so it cannot be paid on
    top of this checkout; one already paid and authorized activates the
    plan instead. A checkout whose card charge never answered (a timeout)
    leaves the plan pending with its subscription at Asaas. If that charge
    went through after all, the plan is activated now instead of charging
    a second time; otherwise the subscription is cancelled so the new
    checkout starts clean. Returns True when the plan turned out paid.
    """
    if plan.gateway_authorization_id:
        try:
            authorization = client.get_pix_automatic_authorization(plan.gateway_authorization_id)
        except asaas_gateway.AsaasAPIError as exc:
            if exc.status_code != 404:
                raise
            # Gone at Asaas: nothing left to withdraw.
            authorization = {"status": "CANCELLED"}
        if authorization.get("status") == PIX_AUTHORIZATION_ACTIVE:
            _activate_plan(plan)
            return True
        if authorization.get("status") not in PIX_AUTHORIZATION_ENDED:
            client.cancel_pix_automatic_authorization(plan.gateway_authorization_id)
        plan.gateway_authorization_id = ""
        plan.save(update_fields=["gateway_authorization_id", "updated_at"])
    if not plan.gateway_subscription_id:
        return False
    payments = client.list_subscription_payments(plan.gateway_subscription_id)
    if any(payment.get("status") in PAID_PAYMENT_STATUSES for payment in payments):
        _activate_plan(plan)
        return True
    client.cancel_subscription(plan.gateway_subscription_id)
    plan.gateway_subscription_id = ""
    plan.save(update_fields=["gateway_subscription_id", "updated_at"])
    return False


def _create_brands(organization, names, owner):
    """The brands typed as new rows at checkout, created once they are paid for."""
    from apps.workspaces.services import create_brand

    return [create_brand(organization, name, owner) for name in names]


def purchase_plan(
    organization,
    *,
    brands,
    cycle,
    card,
    holder,
    remote_ip,
    email="",
    coupon=None,
    workspaces=(),
    new_brand_names=(),
    owner=None,
    client=None,
):
    """Buy ``brands`` brands for the organization and pay the first charge by card.

    ``card`` and ``holder`` go to the gateway as they are (see
    ``AsaasClient.pay_with_card``) and are kept nowhere here. In order:
    the customer, a subscription priced by ``services.quote_plan``, the
    card charge of its first payment on the spot, then the card's token on
    the subscription so every later cycle is charged to it. A ``coupon``
    (already checked by ``services.find_coupon``) prices the subscription
    or, for the first charge only, that charge, and is recorded as used
    once the card is charged. ``workspaces`` are the existing brands ticked
    and ``new_brand_names`` the brands to create (by ``owner``) once paid;
    the plan covers those first.

    Returns the active ``OrganizationPlan``. Raises ``CardRefusedError``
    when the card is declined (the subscription is cancelled, nothing is
    owed), ``CardChallengeRequiredError`` when the issuer wants the payer
    to authenticate, and ``AsaasError`` for anything else. After a
    transport error on the charge itself the plan stays pending with its
    subscription: the webhook activates it if the charge did go through,
    and the next checkout checks before charging again.
    """
    client = client or AsaasClient()
    plan, _created = OrganizationPlan.objects.get_or_create(organization=organization)
    if plan.is_active:
        return plan
    if _settle_earlier_attempt(plan, client):
        # The earlier charge went through after all: what this checkout chose is covered by it.
        chosen = [*workspaces, *_create_brands(organization, new_brand_names, owner)]
        services.sync_plan_coverage(plan, chosen)
        return plan

    quote = services.quote_plan(brands, cycle, coupon)
    profile = ensure_customer(organization, client=client, email=email)
    remote = client.create_subscription(
        customer_id=profile.gateway_customer_id,
        value_cents=quote["charge_cents"],
        cycle=asaas_gateway.CYCLE_YEARLY
        if quote["cycle"] == WorkspaceSubscription.Cycle.YEARLY
        else asaas_gateway.CYCLE_MONTHLY,
        external_reference=f"plan:{plan.pk}",
        description=_plan_description(organization, brands),
    )
    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")

    plan.brands = brands
    plan.billing_cycle = quote["cycle"]
    plan.price_cents = quote["monthly_cents"]
    plan.gateway = GATEWAY
    plan.gateway_customer_id = profile.gateway_customer_id
    plan.gateway_subscription_id = subscription_id
    plan.status = WorkspaceSubscription.Status.PENDING
    plan.coupon = coupon
    plan.save()

    payment = _first_unpaid(client, subscription_id)
    if payment is None:
        raise AsaasError("Asaas created the subscription without a first charge.")
    if quote["first_charge_cents"] != quote["charge_cents"]:
        client.update_payment_value(payment["id"], quote["first_charge_cents"])

    try:
        paid = client.pay_with_card(payment["id"], card=card, holder=holder, remote_ip=remote_ip)
    except asaas_gateway.AsaasAPIError as exc:
        if exc.status_code != 400:
            raise
        # Declined or refused data: nothing was charged. The subscription
        # goes, so no invoice for it reaches the customer.
        client.cancel_subscription(subscription_id)
        plan.gateway_subscription_id = ""
        plan.save(update_fields=["gateway_subscription_id", "updated_at"])
        descriptions = [str(e.get("description") or "") for e in exc.errors if isinstance(e, dict)]
        raise CardRefusedError(" ".join(d for d in descriptions if d) or _("The card was declined.")) from exc

    challenge = paid.get("threeDSecureChallengeUrl") or ""
    if paid.get("status") not in PAID_PAYMENT_STATUSES:
        if isinstance(challenge, str) and challenge.startswith("https://"):
            raise CardChallengeRequiredError(challenge)
        raise CardRefusedError(_("The card was not charged. Try again or use another card."))

    # The charge as first listed, completed by what paying it returned.
    _record_card_charge(organization, {**payment, **paid})
    card_info = paid.get("creditCard") or {}
    token = card_info.get("creditCardToken") or ""
    if token:
        try:
            client.set_subscription_card(subscription_id, card_token=token, remote_ip=remote_ip)
        except AsaasError:
            # Paid all the same; only the renewals would come as invoices.
            logger.exception("Could not keep the card on plan %s's subscription", plan.pk)
    # Kept for the proportional charge of brands added later (add_plan_brands).
    plan.gateway_card_token = token or None
    plan.save(update_fields=["gateway_card_token", "updated_at"])
    logger.info("Organization %s bought a plan of %s brands", organization.pk, brands)
    chosen = [*workspaces, *_create_brands(organization, new_brand_names, owner)]
    return _activate_plan(plan, card=card_info, coupon_saving_cents=quote["coupon_saving_cents"], workspaces=chosen)


def start_pix_plan(
    organization,
    *,
    brands,
    cycle,
    email="",
    coupon=None,
    workspaces=(),
    new_brand_names=(),
    owner=None,
    client=None,
):
    """Buy ``brands`` brands for the organization by Pix Automático.

    Creates a Pix Automático authorization whose QR code pays the first
    charge (``first_charge_cents`` of ``services.quote_plan``) and, once
    paid, authorizes the renewals, which Asaas charges from the next cycle
    through the subscription it creates. The plan stays pending until the
    ``PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED`` webhook activates it
    (``webhooks._apply``). Brands typed as new rows are created now, as
    unpaid brands, so the plan covers them when it activates.

    Returns ``(plan, qr_code)``: ``qr_code`` holds ``image`` (base64 PNG),
    ``payload`` (the copy-and-paste code), ``expires_at`` and
    ``value_cents``, or is None when an earlier attempt turned out paid
    and the plan is active. Raises ``AsaasError`` when Asaas refuses.
    """
    client = client or AsaasClient()
    plan, _created = OrganizationPlan.objects.get_or_create(organization=organization)
    if plan.is_active:
        return plan, None
    if _settle_earlier_attempt(plan, client):
        chosen = [*workspaces, *_create_brands(organization, new_brand_names, owner)]
        services.sync_plan_coverage(plan, chosen)
        return plan, None

    quote = services.quote_plan(brands, cycle, coupon)
    profile = ensure_customer(organization, client=client, email=email)
    yearly = quote["cycle"] == WorkspaceSubscription.Cycle.YEARLY
    authorization = client.create_pix_automatic_authorization(
        customer_id=profile.gateway_customer_id,
        frequency=asaas_gateway.PIX_FREQUENCY_YEARLY if yearly else asaas_gateway.PIX_FREQUENCY_MONTHLY,
        contract_id=plan.pk.hex,
        start_date=services.renewal_date(quote["cycle"]),
        value_cents=quote["charge_cents"],
        first_value_cents=quote["first_charge_cents"],
        description="Connekt Flow",
    )
    authorization_id = authorization.get("id") or ""
    if not authorization_id or not authorization.get("payload"):
        raise AsaasError("Asaas returned a Pix Automático authorization without its QR code.")

    plan.brands = brands
    plan.billing_cycle = quote["cycle"]
    plan.price_cents = quote["monthly_cents"]
    plan.gateway = GATEWAY
    plan.gateway_customer_id = profile.gateway_customer_id
    plan.gateway_authorization_id = authorization_id
    plan.gateway_subscription_id = authorization.get("subscriptionId") or ""
    plan.status = WorkspaceSubscription.Status.PENDING
    plan.coupon = coupon
    plan.card_brand = ""
    plan.card_last4 = ""
    plan.gateway_card_token = None
    plan.save()
    _create_brands(organization, new_brand_names, owner)
    logger.info("Organization %s asked to pay a plan of %s brands by Pix", organization.pk, brands)

    expires = (authorization.get("immediateQrCode") or {}).get("expirationDate") or ""
    return plan, {
        "image": authorization.get("encodedImage") or "",
        "payload": authorization["payload"],
        "expires_at": _asaas_datetime(expires)
        or timezone.now() + datetime.timedelta(seconds=asaas_gateway.PIX_QR_CODE_SECONDS),
        "value_cents": quote["first_charge_cents"],
    }


def _asaas_datetime(raw):
    """An Asaas ``YYYY-MM-DD HH:MM:SS`` (Brasília time) as an aware datetime, or None."""
    try:
        naive = datetime.datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return timezone.make_aware(naive, zoneinfo.ZoneInfo("America/Sao_Paulo"))


def change_plan_brands(plan, brands, *, workspaces=(), new_brand_names=(), owner=None, client=None):
    """Change how many brands an active plan covers, and what it charges.

    The new value applies from the next charge and the workspaces covered
    follow at once. Nothing is charged here: the days until that charge are
    what :func:`add_plan_brands` charges before calling this.
    """
    client = client or AsaasClient()
    # A coupon on every charge keeps applying to the new value.
    quote = services.quote_plan(brands, plan.billing_cycle, plan.coupon)
    client.update_subscription_value(plan.gateway_subscription_id, quote["charge_cents"])
    plan.brands = brands
    plan.price_cents = quote["monthly_cents"]
    plan.save(update_fields=["brands", "price_cents", "updated_at"])
    chosen = [*workspaces, *_create_brands(plan.organization, new_brand_names, owner)]
    services.sync_plan_coverage(plan, chosen)
    return plan


class NothingToCancelError(Exception):
    """None of the brands asked is on the plan and still to be cancelled."""


def cancel_plan_brands(plan, workspaces, *, client=None):
    """Cancel the brands the customer chose on their plan. Returns the day they stay in use until.

    Nothing is refunded and nothing stops at once: the brands stay in use
    until the plan's next charge, the end of the period already paid for
    (``cancels_on``), and ``services.end_cancelled_subscriptions`` closes
    them on that day. What changes now is what the gateway will charge:

    - some brands stay: the subscription's value drops to the plan without
      the cancelled ones, so the next charge already comes without them;
    - no brand stays: the subscription (and a Pix Automático authorization)
      is cancelled at the gateway, so nothing is charged again, and the plan
      itself is marked to end on that day.

    Raises ``NothingToCancelError`` when no brand asked is on the plan, and
    ``AsaasError`` when the gateway refuses, in which case nothing is marked.
    """
    client = client or AsaasClient()
    wanted = {getattr(workspace, "pk", workspace) for workspace in workspaces}
    on_plan = list(WorkspaceSubscription.objects.filter(plan=plan, cancels_on__isnull=True))
    chosen = [row for row in on_plan if row.workspace_id in wanted]
    if not plan.is_active or plan.cancels_on or not chosen:
        raise NothingToCancelError
    ends_on = next_charge_date(plan, client) or timezone.localdate()

    if len(chosen) == len(on_plan):
        for cancel, remote_id in (
            (client.cancel_subscription, plan.gateway_subscription_id),
            (client.cancel_pix_automatic_authorization, plan.gateway_authorization_id),
        ):
            if not remote_id:
                continue
            try:
                cancel(remote_id)
            except asaas_gateway.AsaasAPIError as exc:
                # Already gone at Asaas: the state we were asking for.
                if exc.status_code != 404:
                    raise
        whole = True
    else:
        remaining = services.plan_brands_after_cancellations(plan) - len(chosen)
        quote = services.quote_plan(remaining, plan.billing_cycle, plan.coupon)
        client.update_subscription_value(plan.gateway_subscription_id, quote["charge_cents"])
        whole = False

    with transaction.atomic():
        if whole:
            plan.cancels_on = ends_on
            plan.save(update_fields=["cancels_on", "updated_at"])
            chosen = list(WorkspaceSubscription.objects.filter(plan=plan))
        for row in chosen:
            row.cancels_on = ends_on
            row.save(update_fields=["cancels_on", "updated_at"])
    logger.info("Plan %s: %s brand(s) cancelled, in use until %s", plan.pk, len(chosen), ends_on)
    return ends_on


def keep_plan_brand(plan, workspace, *, client=None):
    """Undo the cancellation of one brand on a plan that carries on: the next charge includes it again.

    Not for a plan cancelled whole: its gateway subscription no longer
    exists, so it is contracted again once it ends. Raises
    ``NothingToCancelError`` when the brand is not waiting to leave the plan.
    """
    client = client or AsaasClient()
    row = WorkspaceSubscription.objects.filter(plan=plan, workspace=workspace, cancels_on__isnull=False).first()
    if row is None or plan.cancels_on or not plan.is_active:
        raise NothingToCancelError
    brands = services.plan_brands_after_cancellations(plan) + 1
    quote = services.quote_plan(brands, plan.billing_cycle, plan.coupon)
    client.update_subscription_value(plan.gateway_subscription_id, quote["charge_cents"])
    row.cancels_on = None
    row.save(update_fields=["cancels_on", "updated_at"])
    return row


def resume_plan(plan, *, remote_ip="", client=None):
    """Undo the cancellation of a whole plan while its paid period still runs: it renews again.

    Cancelling it removed the gateway subscription, so a new one is created
    with its first charge on the day the plan was to end (``cancels_on``)
    and the card the plan was paid with. Nothing is charged now. Only for a
    plan with that card's token kept: a Pix Automático authorization cannot
    be made again without the payer, who contracts anew once the plan ends.

    Raises ``NothingToCancelError`` when the plan is not one cancelled whole
    and still in use with a card on file, and ``AsaasError`` when the
    gateway refuses, in which case the plan stays cancelled.
    """
    if not plan.is_active or not plan.cancels_on or not plan.has_card_on_file:
        raise NothingToCancelError
    client = client or AsaasClient()
    quote = services.quote_plan(plan.brands, plan.billing_cycle, plan.coupon)
    remote = client.create_subscription(
        customer_id=plan.gateway_customer_id,
        value_cents=quote["charge_cents"],
        cycle=asaas_gateway.CYCLE_YEARLY if plan.is_yearly else asaas_gateway.CYCLE_MONTHLY,
        external_reference=f"plan:{plan.pk}",
        description=_plan_description(plan.organization, plan.brands),
        next_due_date=max(plan.cancels_on, timezone.localdate()),
    )
    subscription_id = remote.get("id") or ""
    if not subscription_id:
        raise AsaasError("Asaas returned a subscription without an id.")
    try:
        client.set_subscription_card(subscription_id, card_token=plan.gateway_card_token, remote_ip=remote_ip)
    except AsaasError:
        # Without the card the renewal would come as an invoice nobody asked for.
        client.cancel_subscription(subscription_id)
        raise

    with transaction.atomic():
        plan.gateway_subscription_id = subscription_id
        plan.cancels_on = None
        plan.save(update_fields=["gateway_subscription_id", "cancels_on", "updated_at"])
        WorkspaceSubscription.objects.filter(plan=plan).update(cancels_on=None, updated_at=timezone.now())
    logger.info("Plan %s: cancellation undone, renews on subscription %s", plan.pk, subscription_id)
    return plan


def next_charge_date(plan, client):
    """The day the plan's subscription charges next, asked from the gateway.

    The charge already generated and still unpaid, if there is one;
    otherwise the subscription's ``nextDueDate``, which Asaas defines as the
    due date of the next charge it has yet to generate. None when Asaas
    reports neither.
    """
    pending = _first_unpaid(client, plan.gateway_subscription_id)
    raw = (pending or {}).get("dueDate") or client.get_subscription(plan.gateway_subscription_id).get("nextDueDate")
    try:
        return datetime.date.fromisoformat(str(raw))
    except ValueError:
        return None


def brand_addition_quote(plan, brands, *, client=None):
    """What adding brands up to ``brands`` is charged now (``services.quote_brand_addition``).

    A plan whose next charge the gateway does not report is charged nothing
    now, as when there is nothing to add.
    """
    client = client or AsaasClient()
    next_on = next_charge_date(plan, client) if brands > plan.brands else None
    if next_on is None:
        next_on = timezone.localdate()
    return services.quote_brand_addition(plan, brands, next_on)


def _addition_reference(addition):
    return f"plan-brands:{addition.pk}"


def _settle_earlier_addition(plan, client):
    """Deal with the plan's earlier request to add brands still waiting.

    Asked from the gateway by its reference, so a card charge whose answer
    was lost is found too. One that turned out paid is applied now instead
    of charging again, and True is returned; one still unpaid is withdrawn,
    so it cannot be paid on top of the new request.
    """
    for addition in PlanBrandAddition.objects.filter(plan=plan, status=PlanBrandAddition.Status.PENDING):
        payment = client.find_payment_by_reference(_addition_reference(addition))
        if payment and payment.get("status") in PAID_PAYMENT_STATUSES:
            addition.gateway_payment_id = payment.get("id") or addition.gateway_payment_id
            addition.save(update_fields=["gateway_payment_id"])
            apply_brand_addition(addition, client=client)
            return True
        if payment and payment.get("id"):
            try:
                client.delete_payment(payment["id"])
            except asaas_gateway.AsaasAPIError as exc:
                if exc.status_code != 404:
                    raise
        addition.status = PlanBrandAddition.Status.CANCELED
        addition.settled_at = timezone.now()
        addition.save(update_fields=["status", "settled_at"])
    return False


def add_plan_brands(plan, brands, *, workspaces=(), new_brand_names=(), owner=None, remote_ip="", client=None):
    """Add brands to an active plan, charging the days left until its next charge.

    Returns ``(addition, quote)``: the ``PlanBrandAddition`` when the
    proportional charge still has to be paid (its ``invoice_url`` is where),
    or None when the brands are on the plan already, with what was priced
    (``services.quote_brand_addition``):

    - nothing to charge (see ``services.quote_brand_addition``): the brands
      are added, the new value applies from the next charge;
    - a card on file (``gateway_card_token``): it is charged on the spot and
      the brands are added. ``CardRefusedError`` when it is declined, with
      nothing changed;
    - otherwise (a plan paid by Pix, or by a card whose token was not kept):
      a one-off charge is created and the request waits for it to be paid;
      the webhook then calls :func:`apply_brand_addition`. An earlier request
      still waiting is withdrawn first.

    After a transport error on the card charge the request stays pending:
    nothing is added, and the next call asks the gateway whether that charge
    went through before charging anything (``_settle_earlier_addition``).
    """
    client = client or AsaasClient()
    applied = {"workspaces": workspaces, "new_brand_names": new_brand_names, "owner": owner, "client": client}
    if _settle_earlier_addition(plan, client):
        # The earlier request was paid after all, and is on the plan now.
        plan.refresh_from_db()
        if brands <= plan.brands:
            return None, services.quote_brand_addition(plan, plan.brands, timezone.localdate())
    quote = brand_addition_quote(plan, brands, client=client)
    if quote["amount_cents"] == 0:
        change_plan_brands(plan, brands, **applied)
        return None, quote

    addition = PlanBrandAddition.objects.create(
        plan=plan,
        brands=brands,
        workspace_ids=[str(getattr(workspace, "pk", workspace)) for workspace in workspaces],
        new_brand_names=list(new_brand_names),
        requested_by=owner,
        amount_cents=quote["amount_cents"],
    )
    charge = {
        "customer_id": plan.gateway_customer_id,
        "value_cents": quote["amount_cents"],
        "external_reference": _addition_reference(addition),
        "description": str(
            _("Connekt Flow — brands added to the plan of %(name)s, for the days until the next charge")
            % {"name": plan.organization.name}
        ),
    }
    token = plan.gateway_card_token or ""
    if not token:
        payment = client.create_payment(**charge)
        addition.gateway_payment_id = payment.get("id") or ""
        addition.invoice_url = _https_url(payment.get("invoiceUrl"))
        addition.save(update_fields=["gateway_payment_id", "invoice_url"])
        if not addition.gateway_payment_id or not addition.invoice_url:
            raise AsaasError("Asaas created the charge for the added brands without an invoice.")
        return addition, quote

    try:
        payment = client.create_payment(**charge, card_token=token, remote_ip=remote_ip)
    except asaas_gateway.AsaasAPIError as exc:
        if exc.status_code != 400:
            raise
        # Declined: Asaas keeps no charge, so nothing is owed.
        addition.status = PlanBrandAddition.Status.CANCELED
        addition.settled_at = timezone.now()
        addition.save(update_fields=["status", "settled_at"])
        descriptions = [str(e.get("description") or "") for e in exc.errors if isinstance(e, dict)]
        raise CardRefusedError(" ".join(d for d in descriptions if d) or _("The card was declined.")) from exc
    addition.gateway_payment_id = payment.get("id") or ""
    addition.invoice_url = _https_url(payment.get("invoiceUrl"))
    addition.save(update_fields=["gateway_payment_id", "invoice_url"])
    if payment.get("status") not in PAID_PAYMENT_STATUSES:
        # Under the issuer's analysis: the webhook applies it once confirmed.
        return addition, quote
    _record_card_charge(addition.plan.organization, payment)
    apply_brand_addition(addition, client=client)
    return None, quote


def _record_card_charge(organization, payment):
    """Log a charge the card paid on the spot, so the payment history has it at once.

    The history is read from the gateway's events (``BillingEvent``), and the
    webhook that confirms this charge may take a while or, on a machine the
    gateway cannot reach, never come. The row stands in for that event: same
    type, the payment as Asaas returned it, and an id of our own
    (``checkout:<payment id>``) that no event of the gateway carries. When the
    webhook does arrive the history shows the charge once, by its payment id.
    The affiliate's commission on the charge is written here too, keyed by the
    payment id, so the webhook does not write it twice.
    Never fails the purchase: the charge is already made.
    """
    payment_id = payment.get("id") if isinstance(payment, dict) else ""
    if not payment_id:
        return
    try:
        BillingEvent.objects.get_or_create(
            gateway_event_id=f"checkout:{payment_id}",
            defaults={
                "gateway": GATEWAY,
                "event_type": "PAYMENT_CONFIRMED",
                "status": BillingEvent.Status.PROCESSED,
                "organization": organization,
                "processed_at": timezone.now(),
                "payload": {
                    "payment": {
                        key: payment.get(key)
                        for key in ("id", "value", "invoiceUrl", "subscription", "dueDate", "billingType")
                    }
                },
            },
        )
    except Exception:
        logger.exception("Could not log the card charge %s of organization %s", payment_id, organization.pk)
    # The affiliate who brought the account earns on this charge now, for the
    # same reason; the webhook, when it comes, finds the commission written.
    try:
        affiliate_services.record_payment_event(organization, "PAYMENT_CONFIRMED", {"payment": payment})
    except Exception:
        logger.exception("Could not write the commission of charge %s of organization %s", payment_id, organization.pk)


def _https_url(raw):
    """The gateway's payment page, kept only when it is an https address: people are redirected to it."""
    return raw if isinstance(raw, str) and raw.startswith("https://") else ""


def _organization_owner(organization):
    """An owner of the organization, to own brands created when whoever asked is gone."""
    from apps.members.models import OrgMembership

    membership = (
        OrgMembership.objects.filter(organization=organization, org_role=OrgMembership.OrgRole.OWNER)
        .select_related("user")
        .first()
    )
    return membership.user if membership else None


def apply_brand_addition(addition, *, client=None):
    """Put the brands of a paid request on its plan. Safe to call twice.

    A plan that already covers as many brands (the request was applied, or
    overtaken) is left as it is.
    """
    from apps.workspaces.models import Workspace

    with transaction.atomic():
        addition = (
            PlanBrandAddition.objects.select_for_update(of=("self",))
            .select_related("plan", "requested_by")
            .get(pk=addition.pk)
        )
        if addition.status == PlanBrandAddition.Status.APPLIED:
            return addition.plan
        plan = addition.plan
        if not plan.is_active:
            # Paid for a plan since cancelled or overdue: there is no
            # subscription to raise. Left waiting for someone to look at it.
            logger.error("Addition %s was paid but plan %s is %s", addition.pk, plan.pk, plan.status)
            return plan
        if addition.brands > plan.brands:
            workspaces = list(
                Workspace.objects.filter(pk__in=addition.workspace_ids, organization_id=plan.organization_id)
            )
            change_plan_brands(
                plan,
                addition.brands,
                workspaces=workspaces,
                new_brand_names=addition.new_brand_names,
                owner=addition.requested_by or _organization_owner(plan.organization),
                client=client,
            )
        addition.status = PlanBrandAddition.Status.APPLIED
        addition.settled_at = timezone.now()
        addition.save(update_fields=["status", "settled_at"])
    logger.info("Plan %s now covers %s brands (addition %s paid)", plan.pk, plan.brands, addition.pk)
    return plan
