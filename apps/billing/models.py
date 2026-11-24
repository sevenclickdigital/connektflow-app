"""Models backing per-workspace billing.

The product rule the schema encodes: a workspace is the billable unit.
One plan, every feature included, R$ 50,00/month per workspace. The only
metered resource is disk, and extra disk is sold as a separate add-on row
rather than a column on the subscription — see ``WorkspaceAddon``.

Four roles:

- ``WorkspaceSubscription`` (one per Workspace): the canonical local
  record of what this workspace costs and whether it may be used. Holds
  the gateway (Asaas) customer/subscription ids and the base disk quota.
- ``WorkspaceAddon`` (many per Workspace): anything stacked on top of the
  base subscription. Today only extra storage; tomorrow AI text/image/
  video generation, with no change to the subscription table.
- ``BillingEvent``: append-only log of everything the gateway told us,
  keyed by the gateway's own event id so a redelivered webhook is a
  no-op instead of a double credit.
- ``OrganizationBillingProfile`` (one per Organization): account-level
  billing settings the admin controls, notably how many of the org's
  workspaces are comped.
- ``OrganizationPlan`` (at most one per Organization): a number of brands
  bought together on one gateway subscription, paid by card. It covers that
  many of the organization's workspaces by pointing their
  ``WorkspaceSubscription`` at it, so access checks keep reading one row.

Two unit conventions, held everywhere in this app: money is always an
integer number of cents, disk is always an integer number of bytes.
Neither is ever a float.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.encryption import EncryptedTextField

# ---------------------------------------------------------------------------
# WorkspaceSubscription, one per Workspace
# ---------------------------------------------------------------------------


class WorkspaceSubscription(models.Model):
    """One row per Workspace, holding its billing state.

    Status lifecycle:
    - pending   : row exists, nothing contracted yet. Either a brand-new
                  workspace or an existing one from before billing — both
                  land here and must be subscribed before use.
    - active    : the gateway confirmed payment for the current period.
    - past_due  : the gateway failed to charge. The row stays so a
                  recovered payment flips it back to active without
                  losing the external ids.
    - canceled  : the customer (or an admin) ended the subscription.
                  Terminal unless resubscribed.
    - courtesy  : exempt from billing by admin decision. Counts as
                  active everywhere access is checked, and the billing
                  jobs skip it — there is no gateway subscription behind
                  it, so it must never be charged or dunned.

    ``courtesy`` is a status rather than a boolean beside the status
    because the two are mutually exclusive in practice: a comped
    workspace has no payment state to be in. Keeping it in the same
    column means every "may this workspace be used?" check reads one
    field and cannot forget the exemption.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        PAST_DUE = "past_due", _("Past due")
        CANCELED = "canceled", _("Canceled")
        COURTESY = "courtesy", _("Courtesy")

    # Statuses that grant access. Courtesy is deliberately in here;
    # past_due is deliberately not — a failed charge should block.
    ACTIVE_STATUSES = frozenset({Status.ACTIVE, Status.COURTESY})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="billing_subscription",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    class Cycle(models.TextChoices):
        MONTHLY = "monthly", _("Monthly")
        YEARLY = "yearly", _("Yearly")

    # Price of the base subscription in cents PER MONTH, snapshotted on
    # the row rather than read from settings at render time: raising the
    # list price must not silently re-price workspaces already
    # contracted. A yearly plan stores its monthly equivalent here, so
    # every monthly total and report reads one unit; what is charged each
    # cycle is ``charge_cents``.
    price_cents = models.PositiveIntegerField(default=0)

    # How often the gateway charges. ``db_default`` because the release
    # still running during a deploy inserts rows without this column.
    billing_cycle = models.CharField(
        max_length=8,
        choices=Cycle.choices,
        default=Cycle.MONTHLY,
        db_default=Cycle.MONTHLY,
    )

    # Gateway identifiers. Named for the role, not the vendor — Asaas
    # fills these today (customer_xxx / sub_xxx), and a future gateway
    # swap needs no migration. ``gateway`` records which vendor the ids
    # belong to so a half-migrated table is still unambiguous.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    gateway_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # End of the paid-for window. Access decisions use status, not this
    # field — the gateway tells us when a renewal failed and we flip to
    # past_due; expiring locally on a clock we don't control would cut
    # off a paying customer during a webhook delay.
    current_period_end = models.DateTimeField(null=True, blank=True)

    # Base disk allowance in bytes, snapshotted per workspace for the
    # same reason as price: changing the product default must not shrink
    # the quota of a workspace that is already storing files against it.
    base_storage_bytes = models.PositiveBigIntegerField(default=0)

    # End of the free trial, when one was started. Until then a ``pending``
    # workspace may be used; afterwards it has to be contracted. Read
    # against the clock, so no job has to end a trial.
    trial_ends_at = models.DateTimeField(null=True, blank=True)

    canceled_at = models.DateTimeField(null=True, blank=True)
    # Set when the customer cancels this brand on the organization's plan:
    # the brand stays in use until this day, the plan's next charge, which
    # no longer includes it; then ``services.end_cancelled_subscriptions``
    # takes it off the plan.
    cancels_on = models.DateField(null=True, blank=True)
    # The organization plan covering this workspace, if any. While set, the
    # row's status follows the plan's and the workspace has no gateway
    # subscription of its own.
    plan = models.ForeignKey(
        "billing.OrganizationPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="covered_subscriptions",
    )
    # Why a workspace was comped. Free text and admin-facing only; the
    # billing logic cares about status alone.
    courtesy_note = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_workspace_subscription"

    def __str__(self):
        return f"WorkspaceSubscription({self.workspace_id}, {self.status})"

    @property
    def is_courtesy(self):
        return self.status == self.Status.COURTESY

    @property
    def is_yearly(self):
        return self.billing_cycle == self.Cycle.YEARLY

    @property
    def charge_cents(self):
        """What the gateway charges each cycle for the base subscription."""
        return self.price_cents * 12 if self.is_yearly else self.price_cents

    @property
    def is_settled(self):
        """True when the workspace is paid for or comped."""
        return self.status in self.ACTIVE_STATUSES

    @property
    def in_trial(self):
        """True while a not yet paid workspace is inside its free trial."""
        return (
            self.status == self.Status.PENDING
            and self.trial_ends_at is not None
            and self.trial_ends_at > timezone.now()
        )

    @property
    def trial_expired(self):
        return (
            self.status == self.Status.PENDING
            and self.trial_ends_at is not None
            and self.trial_ends_at <= timezone.now()
        )

    @property
    def is_active(self):
        """True when the workspace may be used.

        Courtesy and a running free trial count as active. Everything
        that gates a feature on "may this workspace be used?" should go
        through here instead of comparing ``status`` to ``"active"``, so
        adding another access-granting state later is a one-line change.
        """
        return self.is_settled or self.in_trial

    @property
    def is_current(self):
        """True when the subscription is active AND the paid period has
        not lapsed.

        Stricter than ``is_active``: this one answers "is the money in
        order right now", which is what a reconcile job or an admin
        report wants. Courtesy rows are always current — nobody is
        charging them, so there is no period to lapse. A NULL
        ``current_period_end`` on a paid row means the gateway hasn't
        reported a period yet; treated as current so a webhook delay
        right after checkout doesn't read as a lapse.
        """
        if self.is_courtesy:
            return True
        if self.status != self.Status.ACTIVE:
            return False
        if self.current_period_end is None:
            return True
        return self.current_period_end >= timezone.now()

    @property
    def extra_storage_bytes(self):
        """Bytes granted by active storage add-ons."""
        from apps.billing.services import extra_storage_bytes

        return extra_storage_bytes(self.workspace_id)

    @property
    def total_storage_bytes(self):
        """Base quota plus every active storage add-on."""
        return self.base_storage_bytes + self.extra_storage_bytes

    @property
    def monthly_total_cents(self):
        """What this workspace bills per month, add-ons included.

        Courtesy workspaces bill nothing regardless of what is attached
        to them, so an admin can comp a workspace that already has
        add-ons without having to strip them first.
        """
        if self.is_courtesy:
            return 0
        from apps.billing.services import addons_monthly_cents

        return self.price_cents + addons_monthly_cents(self.workspace_id)


# ---------------------------------------------------------------------------
# WorkspaceAddon, stackable extras
# ---------------------------------------------------------------------------


class WorkspaceAddon(models.Model):
    """An extra contracted on top of a workspace's base subscription.

    One row per contracted add-on, never a column on
    ``WorkspaceSubscription`` — the product roadmap adds AI text/image/
    video generation next, and each of those should be a new ``Kind``
    value plus a row, not a schema change to the subscription every
    agency's billing depends on.

    ``quantity`` is interpreted per kind: for ``extra_storage`` it is a
    number of GB. ``unit_price_cents`` is the price that applied at the
    moment of contracting and is never rewritten — the list price in
    settings may change, but an existing customer's bill must not move
    retroactively. Recomputing a historical charge from today's settings
    is exactly the bug this column exists to prevent.
    """

    class Kind(models.TextChoices):
        EXTRA_STORAGE = "extra_storage", _("Extra storage")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        CANCELED = "canceled", _("Canceled")

    # Bytes per GB as billed. Decimal GB (10^9) rather than binary GiB
    # (2^30) because the customer is buying what the price list calls
    # "1 GB" and we must not quietly hand out 7% more than we sell.
    BYTES_PER_GB = 1_000_000_000

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="billing_addons",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    quantity = models.PositiveIntegerField(default=0)
    unit_price_cents = models.PositiveIntegerField(default=0)

    # Add-ons may be billed as their own gateway subscription (Asaas has
    # no concept of a line item on an existing subscription), so they
    # carry their own external id.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_workspace_addon"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "kind", "status"]),
        ]

    def __str__(self):
        return f"WorkspaceAddon({self.workspace_id}, {self.kind}, {self.quantity})"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def total_price_cents(self):
        """Monthly cost of this add-on at the price it was sold for."""
        return self.quantity * self.unit_price_cents

    @property
    def storage_bytes(self):
        """Bytes this add-on grants, or 0 for non-storage kinds."""
        if self.kind != self.Kind.EXTRA_STORAGE:
            return 0
        return self.quantity * self.BYTES_PER_GB


# A proxy over the same table, so the storage case reads clearly at call
# sites ("WorkspaceStorageAddon") while the roadmap's future add-on kinds
# keep sharing one physical table and one set of billing jobs.
class WorkspaceStorageAddonManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(kind=WorkspaceAddon.Kind.EXTRA_STORAGE)

    def create(self, **kwargs):
        kwargs.setdefault("kind", WorkspaceAddon.Kind.EXTRA_STORAGE)
        return super().create(**kwargs)


class WorkspaceStorageAddon(WorkspaceAddon):
    """Extra GB of disk contracted for a workspace.

    A proxy, not a table: storage is the only add-on today but the
    billing jobs, the gateway sync, and the admin are all written
    against ``WorkspaceAddon``, and adding AI add-ons later must not
    mean a second code path. ``quantity`` is GB; ``unit_price_cents`` is
    the per-GB price frozen at contracting time.
    """

    objects = WorkspaceStorageAddonManager()

    class Meta:
        proxy = True
        verbose_name = _("workspace storage add-on")
        verbose_name_plural = _("workspace storage add-ons")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set in __init__ as well as save() so ``storage_bytes`` and the
        # other kind-dependent properties are already correct on an
        # unsaved instance — a caller computing a quote before writing
        # the row would otherwise read 0 bytes.
        self.kind = WorkspaceAddon.Kind.EXTRA_STORAGE

    def save(self, *args, **kwargs):
        # The proxy only ever addresses storage rows; forcing the kind
        # here keeps a caller that forgot it from writing a row the
        # proxy's own manager would then filter out of existence.
        self.kind = WorkspaceAddon.Kind.EXTRA_STORAGE
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# BillingEvent, gateway webhook log
# ---------------------------------------------------------------------------


class BillingEvent(models.Model):
    """One row per event the payment gateway sent us.

    Exists for two reasons that happen to need the same table:

    - Idempotency. Gateways redeliver: Asaas retries a webhook until it
      gets a 200, and a slow response means the same event arrives
      twice. ``gateway_event_id`` is unique, so the handler can insert
      first and treat an IntegrityError as "already processed" — the
      check and the claim are then one atomic operation, which a
      ``SELECT ... if not exists`` cannot be.
    - Audit. When a customer disputes a charge, the raw payload we acted
      on is the only honest record of why we did what we did.

    ``processed_at`` stays NULL until the handler finishes, so a crash
    mid-processing leaves a claimed-but-unprocessed row that a sweeper
    can find rather than an event silently lost.
    """

    class Status(models.TextChoices):
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        IGNORED = "ignored", _("Ignored")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gateway = models.CharField(max_length=32, blank=True, default="")
    # The gateway's own event identifier (Asaas sends ``evt_...``). The
    # unique constraint is the whole point of this column.
    gateway_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Nullable: an event may arrive for a subscription we never recorded
    # (a customer created directly in the gateway dashboard, or a row
    # since deleted). Dropping the event instead would lose the audit
    # trail precisely when it is most needed.
    subscription = models.ForeignKey(
        "billing.WorkspaceSubscription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_events",
    )
    addon = models.ForeignKey(
        "billing.WorkspaceAddon",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_events",
    )

    # Whose money the event is about, whatever it paid for: the plan, a
    # brand on its own, an add-on, brands added to the plan. The payment
    # history of the organization is read from here, so it survives a brand
    # being archived or deleted and a subscription being canceled.
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_events",
        db_index=False,
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "billing_event"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"], name="billing_event_org_created")]

    def __str__(self):
        return f"BillingEvent({self.gateway_event_id}, {self.status})"


# ---------------------------------------------------------------------------
# OrganizationBillingProfile, account-level billing settings
# ---------------------------------------------------------------------------


class OrganizationBillingProfile(models.Model):
    """Account-level billing settings for one Organization.

    Chosen over adding the fields straight onto ``Organization`` because
    this is the account's billing *relationship*, not part of its
    identity: it grows with every gateway concern (tax id, payment
    method, dunning state, invoice address) and none of that belongs on
    a model that every request already loads. A separate table also
    keeps billing columns out of the org's query plan and lets this app
    be reasoned about — and migrated — on its own.

    ``courtesy_workspace_quota`` is the account-level comp the product
    owner asked for: "this organization gets N workspaces free". It is a
    quota rather than a flag so an agency can be given, say, three comped
    workspaces while paying for the rest. Which specific workspaces
    consume the quota is deliberately not recorded here — the quota is
    spent by counting workspaces whose subscription sits in
    ``courtesy``, so an admin moves the comp between workspaces by
    flipping statuses and never has to keep two tables in agreement.

    ``courtesy_expires_on`` is the deadline on that comp. It sits on the
    account rather than on each ``WorkspaceSubscription`` for the same
    reason the quota does: the operator grants "this account is free
    until the 30th", one decision, and the expiry job then has one date
    to read instead of having to agree with N per-workspace copies of
    it. NULL means open-ended, which is what every row granted before
    this field existed means — see the field's own comment.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )

    # The payer's CPF or CNPJ (digits only) and phone. Asked when a free
    # trial starts or at the first contract, and sent to the gateway when
    # the customer is created there. The document is also what stops one
    # person from taking a trial per account.
    tax_id = models.CharField(max_length=14, blank=True, default="", db_default="")
    phone = models.CharField(max_length=20, blank=True, default="", db_default="")
    # Whose name the invoices carry. Empty = the organization's name.
    payer_name = models.CharField(max_length=255, blank=True, default="", db_default="")
    # When this account used its one free trial. NULL = never.
    trial_started_at = models.DateTimeField(null=True, blank=True)
    # The document that trial was taken with. Kept apart from ``tax_id``,
    # which the customer may change afterwards: the trial stays spent for
    # the document that spent it.
    trial_tax_id = models.CharField(max_length=14, blank=True, default="", db_default="")
    # The phone and the (normalized) email of whoever started that trial,
    # kept for the same reason: another account presenting either one, or
    # an address at the same company domain, has had its trial already.
    trial_phone = models.CharField(max_length=20, blank=True, default="", db_default="")
    trial_email = models.CharField(max_length=254, blank=True, default="", db_default="")

    # How many of this org's workspaces may be comped. 0 = none.
    courtesy_workspace_quota = models.PositiveIntegerField(default=0)
    courtesy_note = models.CharField(max_length=255, blank=True, default="")

    # The day the exemption stops. NULL means "no end date" — which is
    # every row that predates this field, so an existing open-ended
    # courtesy is never silently given a deadline by the migration.
    #
    # A DateField, not a DateTimeField: the operator grants "free until
    # the 30th", never "until 14:35". Expiry is therefore evaluated as
    # "today is past that day", which is also why the expiry job can run
    # once a day instead of on the minute.
    courtesy_expires_on = models.DateField(null=True, blank=True)
    courtesy_granted_at = models.DateTimeField(null=True, blank=True)
    courtesy_granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_billing_courtesies",
    )

    # One gateway customer per account even though billing is per
    # workspace: the payer is the organization, and Asaas keys payment
    # methods to the customer. Each workspace subscription then points
    # at this same customer id.
    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_organization_profile"

    def __str__(self):
        return f"OrganizationBillingProfile({self.organization_id})"

    @property
    def courtesy_workspaces_used(self):
        """How many of this org's workspaces are currently comped."""
        return WorkspaceSubscription.objects.filter(
            workspace__organization_id=self.organization_id,
            status=WorkspaceSubscription.Status.COURTESY,
        ).count()

    @property
    def courtesy_workspaces_remaining(self):
        """Unused comp slots, floored at 0.

        Can be floored rather than negative when an admin lowers the
        quota below what is already granted; the existing comps stand
        (we don't start charging retroactively) and the account simply
        has no slots left.
        """
        return max(0, self.courtesy_workspace_quota - self.courtesy_workspaces_used)

    @property
    def has_courtesy_available(self):
        return self.courtesy_workspaces_remaining > 0

    @property
    def courtesy_has_deadline(self):
        return self.courtesy_expires_on is not None

    @property
    def courtesy_is_expired(self):
        """True once the granted day has passed.

        Strictly ``<``, so a comp that expires "on the 30th" is still
        honoured all through the 30th and only lapses on the 31st. An
        operator typing today's date means "free for today", not "free
        for none of today".
        """
        if self.courtesy_expires_on is None:
            return False
        return self.courtesy_expires_on < timezone.localdate()

    @property
    def courtesy_days_remaining(self):
        """Whole days left on the deadline, or None when open-ended.

        Negative once lapsed, so a screen can tell "expires tomorrow"
        from "expired last week" without recomputing the subtraction.
        """
        if self.courtesy_expires_on is None:
            return None
        return (self.courtesy_expires_on - timezone.localdate()).days


# ---------------------------------------------------------------------------
# OrganizationPlan, brands bought together
# ---------------------------------------------------------------------------


class OrganizationPlan(models.Model):
    """A number of brands an organization pays for on one gateway subscription.

    Bought on the checkout page with a card: the first charge is paid on the
    spot and the card stays on the subscription, so every later cycle is
    charged without the customer coming back. ``brands`` workspaces of the
    organization are covered; each covered ``WorkspaceSubscription`` points
    here through ``plan`` and mirrors this row's status
    (``services.sync_plan_coverage``), so the access checks, the lock and
    the billing pages keep reading the workspace row alone.

    ``price_cents`` is the monthly equivalent of what is charged each cycle,
    discounts included, fixed at purchase like a workspace's price. Status
    uses the workspace subscription's values; ``courtesy`` never applies.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_plan",
    )
    status = models.CharField(
        max_length=16,
        choices=WorkspaceSubscription.Status.choices,
        default=WorkspaceSubscription.Status.PENDING,
        db_index=True,
    )
    brands = models.PositiveIntegerField(default=1)
    billing_cycle = models.CharField(
        max_length=8,
        choices=WorkspaceSubscription.Cycle.choices,
        default=WorkspaceSubscription.Cycle.MONTHLY,
    )
    price_cents = models.PositiveIntegerField(default=0)

    gateway = models.CharField(max_length=32, blank=True, default="")
    gateway_customer_id = models.CharField(max_length=255, blank=True, default="")
    gateway_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # The Pix Automático authorization of a plan paid by Pix: its QR code pays
    # the first charge and authorizes the renewals. Empty when paid by card.
    gateway_authorization_id = models.CharField(max_length=64, blank=True, default="", db_default="")
    # Shown on the billing page ("Mastercard ending in 8829"). The card
    # itself stays at the gateway, which keeps only a token on its side.
    # The coupon paid with, kept so a resize prices its "every charge"
    # discount again.
    coupon = models.ForeignKey("billing.Coupon", on_delete=models.SET_NULL, null=True, blank=True, related_name="plans")
    card_brand = models.CharField(max_length=32, blank=True, default="")
    card_last4 = models.CharField(max_length=4, blank=True, default="")
    # The gateway's token of the card the plan was paid with, for the charge
    # of brands added between two cycles (``contracting.add_plan_brands``).
    # Only the gateway can turn it into a charge, and only for this customer.
    # NULL for a plan paid by Pix, or by card before the token was kept.
    gateway_card_token = EncryptedTextField(null=True, blank=True)

    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    # Set when the customer cancels the whole subscription: it no longer
    # renews (the gateway subscription is gone) and the plan stays active
    # until this day, the end of the period already paid for, when
    # ``services.end_cancelled_subscriptions`` closes it.
    cancels_on = models.DateField(null=True, blank=True)
    # When the owner was told that the organization, archived since
    # ``canceled_at``, is about to be removed. It counts only when later
    # than ``canceled_at``; nothing is removed without it
    # (``services.remove_expired_archives``).
    removal_warned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_organization_plan"

    def __str__(self):
        return f"OrganizationPlan({self.organization_id}, {self.brands}, {self.status})"

    @property
    def is_yearly(self):
        return self.billing_cycle == WorkspaceSubscription.Cycle.YEARLY

    @property
    def has_card_on_file(self):
        """Whether brands added between two cycles can be charged to the plan's card on the spot."""
        return bool(self.gateway_card_token)

    @property
    def charge_cents(self):
        """What the gateway charges each cycle."""
        return self.price_cents * 12 if self.is_yearly else self.price_cents

    @property
    def is_active(self):
        return self.status == WorkspaceSubscription.Status.ACTIVE


class PlanBrandAddition(models.Model):
    """Brands asked to be added to an active plan, waiting for their proportional charge.

    Adding brands between two cycles is charged for the days left until the
    plan's next charge. When that charge cannot be taken on the spot (a plan
    paid by Pix, or by a card whose token was not kept), the request waits
    here with the one-off charge at the gateway; the webhook that reports it
    paid applies it (``contracting.apply_brand_addition``). A plan has at
    most one pending request: a new one replaces it.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPLIED = "applied", _("Applied")
        CANCELED = "canceled", _("Canceled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(OrganizationPlan, on_delete=models.CASCADE, related_name="brand_additions")
    # How many brands the plan covers once this is applied.
    brands = models.PositiveIntegerField()
    # The existing brands ticked (ids as strings) and the brands to create.
    workspace_ids = models.JSONField(default=list, blank=True)
    new_brand_names = models.JSONField(default=list, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    amount_cents = models.PositiveIntegerField()
    gateway_payment_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    invoice_url = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "billing_plan_brand_addition"
        ordering = ["-created_at"]

    def __str__(self):
        return f"PlanBrandAddition({self.plan_id}, {self.brands}, {self.status})"


# ---------------------------------------------------------------------------
# Coupon, a discount the operator hands to a customer
# ---------------------------------------------------------------------------


class Coupon(models.Model):
    """A discount code created on the staff Coupons screen and typed at checkout.

    ``kind`` says how ``value`` reads: whole percent off, or cents off each
    charge. ``duration`` says which charges it touches: only the first, or
    every one for as long as the plan lasts. Usable while ``is_active``,
    until ``valid_until`` (inclusive) and up to ``max_redemptions``
    organizations, each organization once (``CouponRedemption``).
    """

    class Kind(models.TextChoices):
        PERCENT = "percent", _("Percentage")
        AMOUNT = "amount", _("Fixed amount")

    class Duration(models.TextChoices):
        FIRST = "first", _("First charge only")
        FOREVER = "forever", _("Every charge")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Stored upper case; typed in any case at checkout.
    code = models.CharField(max_length=32, unique=True)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PERCENT)
    value = models.PositiveIntegerField()
    duration = models.CharField(max_length=8, choices=Duration.choices, default=Duration.FIRST)
    valid_until = models.DateField(null=True, blank=True)
    # NULL = no limit.
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_coupons",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_coupon"
        ordering = ["-created_at"]

    def __str__(self):
        return self.code


class CouponRedemption(models.Model):
    """An organization's use of a coupon, written when its checkout is paid."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="redemptions")
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="coupon_redemptions",
    )
    # What the coupon took off the first charge, in cents.
    discount_cents = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_coupon_redemption"
        constraints = [
            models.UniqueConstraint(fields=["coupon", "organization"], name="billing_coupon_once_per_org"),
        ]


# ---------------------------------------------------------------------------
# ProductPrice, the editable price list
# ---------------------------------------------------------------------------


class ProductPrice(models.Model):
    """One row per editable figure on the product's price list.

    Why a table in *this* app and not ``settings_manager``: ``OrgSetting``
    and ``WorkspaceSetting`` are both scoped to a tenant, and the price
    list is the opposite of tenant-scoped — it is the product's own
    number, one per installation. Storing it under an organization would
    either need a fake "house" organization or would let one customer's
    row decide what another is quoted.

    Why key/value and not a one-row table with three columns: the
    roadmap's next add-ons (AI text/image/video) each arrive as another
    list price, and a column per price means a migration per price on the
    table the operator's screen reads. A key is a data change.

    Why ``value`` is a ``PositiveBigIntegerField`` and not JSON: every
    figure on this list is a non-negative integer in a documented unit
    (cents for money, bytes for disk — the convention this whole app
    holds). A JSON column would accept ``"50,00"`` and a float, which is
    precisely the garbage this is meant to refuse.

    **A row here is a list price and nothing else.** It is read when a
    subscription or an add-on is contracted, and the resulting row keeps
    its own ``price_cents`` / ``base_storage_bytes`` / ``unit_price_cents``.
    Editing a row here therefore cannot re-price anything already sold —
    see ``apps.billing.services`` for the read path and
    ``ProductPriceChange`` for who changed what.

    Absence is meaningful: no row means "nobody has set this", and the
    reader falls back to the deployment setting. That is what keeps a
    fresh installation working and preserves the configured default
    instead of baking a copy of it into the database at migrate time.
    """

    class Key(models.TextChoices):
        # Values are the public identifiers: they appear in form fields
        # and in the cache key, so they are chosen to read as units.
        WORKSPACE_PRICE_CENTS = "workspace_price_cents", _("Price per workspace (cents)")
        BASE_STORAGE_BYTES = "base_storage_bytes", _("Disk included (bytes)")
        STORAGE_PRICE_PER_GB_CENTS = "storage_price_per_gb_cents", _("Price per extra GB (cents)")
        # The billing rules, editable on the same screen and recorded in the
        # same trail. Whole numbers too: a percentage, a number of days, a
        # 0/1 switch, and up to three volume-discount tiers as "from which
        # paid workspace" plus "percent" pairs (0 and 0 = an unused tier).
        ANNUAL_DISCOUNT_PERCENT = "annual_discount_percent", _("Yearly plan discount (%)")
        TRIAL_DAYS = "trial_days", _("Free trial (days)")
        REQUIRE_SUBSCRIPTION = "require_subscription", _("Subscription required (1 = yes)")
        VOLUME_1_FROM = "volume_1_from", _("Volume discount 1: from paid workspace")
        VOLUME_1_PERCENT = "volume_1_percent", _("Volume discount 1 (%)")
        VOLUME_2_FROM = "volume_2_from", _("Volume discount 2: from paid workspace")
        VOLUME_2_PERCENT = "volume_2_percent", _("Volume discount 2 (%)")
        VOLUME_3_FROM = "volume_3_from", _("Volume discount 3: from paid workspace")
        VOLUME_3_PERCENT = "volume_3_percent", _("Volume discount 3 (%)")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, choices=Key.choices, unique=True)
    value = models.PositiveBigIntegerField()

    # Who last moved this number, kept denormalized on the row so the
    # screen can say "last changed by X" without walking the history.
    # The history is still the record of truth — see ProductPriceChange.
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_billing_prices",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_product_price"

    def __str__(self):
        return f"ProductPrice({self.key}={self.value})"


class ProductPriceChange(models.Model):
    """Append-only record of one edit to the price list.

    Separate from ``ProductPrice`` because the two answer different
    questions and have different lifetimes: the price row is "what do we
    charge now" and is overwritten, while this is "who decided to charge
    that, when, and what did it replace" and is never rewritten. Folding
    the history into the price row would mean one row per price *per
    edit* and a screen that has to work out which one is current.

    ``previous_value`` is nullable, and NULL means "there was no row
    before" — the first edit of a figure that until then came from the
    deployment setting. Recording the setting's value there instead would
    claim the operator changed a number somebody had set, which nobody
    had.

    ``changed_by`` is SET_NULL rather than CASCADE: a deleted operator
    account must not take the record of their pricing decisions with it.
    ``changed_by_email`` keeps the identification readable after that.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, choices=ProductPrice.Key.choices, db_index=True)
    previous_value = models.PositiveBigIntegerField(null=True, blank=True)
    new_value = models.PositiveBigIntegerField()

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_price_changes",
    )
    # Snapshotted so the trail still names a human after the account is
    # gone. The same reason every sold row snapshots its price.
    changed_by_email = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "billing_product_price_change"
        ordering = ["-created_at"]

    def __str__(self):
        return f"ProductPriceChange({self.key}, {self.previous_value}→{self.new_value})"

    @property
    def is_first_setting(self):
        """True when this edit replaced no stored row, only a default.

        A property rather than ``{% if change.previous_value == None %}``
        in the template: Django templates have no None literal, and the
        alternatives (``|default_if_none`` on a number, truthiness) both
        treat a stored 0 — a real previous price — as "there was none".
        """
        return self.previous_value is None
