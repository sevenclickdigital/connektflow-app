"""Billing screens: contracting, the workspace billing page, staff admin.

URL surface:

- ``/workspace/<workspace_id>/billing/``                  : billing overview
- ``/workspace/<workspace_id>/billing/subscribe/``        : contracting modal
- ``/workspace/<workspace_id>/billing/subscribe/confirm/``: POST, contracts
- ``/workspace/<workspace_id>/billing/storage/quote/``    : HTMX price quote
- ``/workspace/<workspace_id>/billing/storage/``          : POST, buys GB
- ``/billing/staff/``                                     : customers
- ``/billing/staff/plans/``                               : plan and prices
- ``/billing/staff/plans/prices/``                        : POST, list prices
- ``/billing/staff/subscriptions/``                       : subscriptions
- ``/billing/staff/impersonations/``                      : access log
- ``/billing/staff/courtesy/<target_workspace_id>/``      : POST, comp toggle
- ``/billing/staff/quota/<org_id>/``                      : POST, org quota
- ``/billing/staff/accounts/<org_id>/courtesy/``          : POST, timed comp
- ``/billing/staff/accounts/<org_id>/edit/``              : POST, name/e-mail
- ``/billing/staff/users/<target_user_id>/reset-password/``: POST, allauth mail
- ``/billing/staff/users/<target_user_id>/active/``       : POST, de/reactivate

Two things this module is careful about, because both are states the
product is actually in today:

1. **Asaas has no credentials in most environments.** ``AsaasNotConfigured``
   is therefore the *normal* outcome of pressing "Contract", not an
   incident. It is caught separately from ``AsaasError`` and reported as
   "billing isn't set up yet" with the local row left pending — never a
   500, never a stack trace.
2. **Money is cents everywhere.** Nothing here divides by 100; the
   ``brl`` filter in ``templatetags/billing_extras.py`` is the only place
   that formats, so a cents value can't leak into a template as "5000".
"""

from __future__ import annotations

import functools
import logging
import re

from allauth.account.forms import ResetPasswordForm
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Prefetch, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.accounts import impersonation
from apps.accounts.impersonation import client_ip
from apps.accounts.models import ImpersonationSession, User
from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError, AsaasError, AsaasNotConfigured
from apps.billing.models import BillingEvent, Coupon, ProductPrice, WorkspaceAddon, WorkspaceSubscription
from apps.billing.storage_usage import storage_status
from apps.billing.webhooks import PAID_EVENTS, UNPAID_EVENTS
from apps.common.validators import clean_display_name
from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.organizations.views import needs_about, needs_welcome
from apps.social_accounts.models import PlatformVisibility
from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

# Quantities offered in the add-on picker. A shortlist rather than a free
# number field: the product owner's examples (5, 10, 50) are the sizes
# anyone actually buys, and a fixed set keeps the quote the customer sees
# and the quote we charge on the same rails. "Other" is still allowed —
# the POST validates any integer in range — so this is a convenience, not
# a cap.
STORAGE_ADDON_CHOICES = (5, 10, 25, 50, 100)

# Hard bounds on a storage purchase. Upper bound exists so a fat-fingered
# "5000" is a validation error instead of a R$ 10.000 subscription.
STORAGE_ADDON_MIN_GB = 1
STORAGE_ADDON_MAX_GB = 1000

# How many impersonation rows the access log shows. A window rather than
# pagination: takeovers are rare, and the question the page answers ("was
# anyone inside this account recently") is a recency question. Also what
# keeps the page off a full-table scan if that ever stops being true.
IMPERSONATION_LOG_LIMIT = 200


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_workspace(request, workspace_id):
    """Resolve the workspace and enforce membership.

    Mirrors ``apps.composer.views._get_workspace`` rather than the media
    library's middleware-based check: billing pages are reachable by
    direct link (the contracting redirect right after creation), so they
    cannot assume ``request.workspace`` has already been set to this one.
    """
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _can_manage_billing(request, workspace):
    """Whether this member may spend the organization's money.

    Owner or manager of the workspace, or an owner/admin of the org.
    Contributors and clients can see the storage meter (they hit the 90%
    warning too) but must not be able to contract a charge.
    """
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership and membership.workspace_role in (
        WorkspaceMembership.WorkspaceRole.OWNER,
        WorkspaceMembership.WorkspaceRole.MANAGER,
    ):
        return True
    org_membership = getattr(request, "org_membership", None)
    return bool(
        org_membership
        and org_membership.organization_id == workspace.organization_id
        and org_membership.org_role in ("owner", "admin")
    )


def _require_manage_billing(request, workspace):
    if not _can_manage_billing(request, workspace):
        raise PermissionDenied("You may not manage billing for this workspace.")


def _parse_quantity_gb(raw):
    """Validated GB quantity from form input, or None.

    Returns None rather than raising so each caller decides between a
    message-and-redirect (the POST) and a zeroed quote (the HTMX
    preview).
    """
    try:
        quantity = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if quantity < STORAGE_ADDON_MIN_GB or quantity > STORAGE_ADDON_MAX_GB:
        return None
    return quantity


def _storage_quote(quantity_gb):
    """What ``quantity_gb`` extra GB costs per month, in cents.

    The one place the add-on price is computed, so the figure shown in
    the picker and the figure written onto the add-on row cannot drift.
    """
    unit = services.storage_price_per_gb_cents()
    return {
        "quantity_gb": quantity_gb,
        "unit_price_cents": unit,
        "total_cents": quantity_gb * unit,
    }


def _gateway_configured():
    return bool(getattr(settings, "ASAAS_API_KEY", ""))


def _needs_tax_id(workspace):
    """Whether contracting has to ask for the payer's CPF or CNPJ.

    Asaas wants it once, to create the customer. Not asked while the
    gateway is unconfigured (nothing would be sent anywhere), nor when
    the account already gave it to start a free trial.
    """
    if not _gateway_configured() or contracting.has_customer(workspace.organization):
        return False
    return not services.ensure_billing_profile(workspace.organization).tax_id


def _posted_tax_id(request, workspace):
    """The CPF/CNPJ digits from a contracting form.

    Returns ``(digits, error)``. ``digits`` is "" when the document is not
    needed; ``error`` is a message for the customer when it is needed and
    missing or does not pass its check digits.
    """
    if not _needs_tax_id(workspace):
        return "", None
    digits = services.valid_tax_id(request.POST.get("tax_id"))
    if digits:
        return digits, None
    return "", _("Enter a valid CPF or CNPJ for the invoice.")


def _display_phone(digits):
    """Stored phone digits as people write them: (11) 98888-7777."""
    if len(digits) not in (10, 11):
        return digits
    return f"({digits[:2]}) {digits[2:-4]}-{digits[-4:]}"


def _gateway_refusal(exc):
    """What Asaas said was wrong with the request, for the customer.

    Asaas describes a refused field in Portuguese ("O CPF ou CNPJ
    informado é inválido"), which is more use to the customer than a
    generic failure. "" for anything that is not a refusal of the data.
    """
    if not isinstance(exc, AsaasAPIError) or exc.status_code != 400:
        return ""
    descriptions = [str(error.get("description") or "") for error in exc.errors if isinstance(error, dict)]
    return " ".join(d for d in descriptions if d)


def _quotes(workspace):
    """Today's price of contracting this workspace, per cycle on offer.

    ``yearly`` is absent while no yearly discount is configured, which is
    what hides the choice on the contracting page.
    """
    cycles = WorkspaceSubscription.Cycle
    quotes = {"monthly": services.quote_workspace(workspace, cycles.MONTHLY)}
    if services.annual_discount_percent() > 0:
        quotes["yearly"] = services.quote_workspace(workspace, cycles.YEARLY)
    return quotes


def _billing_context(request, workspace):
    """Everything both the overview and the contracting modal need."""
    subscription = services.ensure_subscription(workspace)
    status = storage_status(workspace)
    addons = list(
        WorkspaceAddon.objects.filter(
            workspace=workspace,
            kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
        ).exclude(status=WorkspaceAddon.Status.CANCELED)
    )
    return {
        "workspace": workspace,
        "subscription": subscription,
        "storage": status,
        "addons": addons,
        "monthly_total_cents": services.workspace_monthly_total_cents(workspace),
        "storage_price_per_gb_cents": services.storage_price_per_gb_cents(),
        "storage_addon_choices": STORAGE_ADDON_CHOICES,
        # Priced for the amount pre-selected in the picker, so the first
        # paint already shows a figure instead of an empty slot that only
        # fills after the customer touches the input.
        "quote": _storage_quote(STORAGE_ADDON_CHOICES[0]),
        "min_gb": STORAGE_ADDON_MIN_GB,
        "max_gb": STORAGE_ADDON_MAX_GB,
        "can_manage_billing": _can_manage_billing(request, workspace),
        # Lets the page say "billing isn't configured yet" up front rather
        # than only after the customer presses Contract and fails.
        "gateway_configured": _gateway_configured(),
        "needs_tax_id": _needs_tax_id(workspace),
        "quotes": _quotes(workspace),
        # A storage add-on contracted and not paid yet has an invoice too.
        "addon_awaiting_payment": any(
            addon.status == WorkspaceAddon.Status.PENDING and addon.gateway_subscription_id for addon in addons
        ),
        # Contracted at the gateway and not paid yet: the customer has an
        # invoice to open rather than a contract to sign.
        "awaiting_payment": bool(subscription.gateway_subscription_id)
        and subscription.status in (WorkspaceSubscription.Status.PENDING, WorkspaceSubscription.Status.PAST_DUE),
        "subscription_required": services.subscription_required(),
        "trial_available": services.trial_available(workspace, request.user),
        "trial_days": services.trial_days(),
        "payer_known": services.payer_identity_known(workspace.organization),
    }


# ---------------------------------------------------------------------------
# Workspace-facing screens
# ---------------------------------------------------------------------------


@login_required
@require_GET
def workspace_billing(request, workspace_id):
    """Subscription status, monthly cost, disk meter, add-on picker."""
    workspace = _get_workspace(request, workspace_id)
    context = _billing_context(request, workspace)
    context["settings_active"] = "billing"
    return render(request, "billing/workspace_billing.html", context)


@login_required
@require_GET
def account_billing(request):
    """Account centre → Billing: every brand whose billing this user manages.

    Brands of the organization in use when they are its owner/admin, plus
    brands they own or manage there or in organizations they are only a
    guest of. Brands of their other organizations are left to that
    organization's own page. Read
    only: rows are not created here, so a brand with no subscription row yet
    shows as pending, which is what it is.
    """
    admin_org_ids = OrgMembership.objects.filter(
        user=request.user, org_role__in=(OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN)
    ).values_list("organization_id", flat=True)
    managed_ids = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace_role__in=(WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER),
    ).values_list("workspace_id", flat=True)
    # The user's other organizations have this same page of their own, reached
    # by opening one of their brands; mixing their brands in here would put
    # them under the wrong organization's name and payer.
    current_org_id = request.org.id if request.org else None
    other_org_ids = [m.organization_id for m in request.org_memberships if m.organization_id != current_org_id]
    workspaces = (
        Workspace.objects.filter(is_archived=False)
        .filter(Q(organization_id__in=admin_org_ids) | Q(id__in=managed_ids))
        .exclude(organization_id__in=other_org_ids)
        .select_related("organization", "billing_subscription")
        .order_by("organization__name", "name")
    )
    member_of = set(WorkspaceMembership.objects.filter(user=request.user).values_list("workspace_id", flat=True))
    list_price_cents = services.workspace_price_cents()
    rows = []
    for ws in workspaces:
        sub = getattr(ws, "billing_subscription", None)
        if sub is not None and sub.is_settled:
            state = "active"
        elif sub is not None and sub.in_trial:
            state = "trial"
        else:
            state = "inactive"
        rows.append(
            {
                "workspace": ws,
                "subscription": sub,
                "state": state,
                # A brand never contracted has no price of its own yet: it
                # would be contracted at today's list price.
                "monthly_total_cents": (
                    services.workspace_monthly_total_cents(ws)
                    if sub is not None and (sub.is_settled or sub.gateway_subscription_id)
                    else list_price_cents
                ),
                # Billing pages need brand membership (_get_workspace).
                "can_open": ws.id in member_of,
                # Contracted at the gateway and waiting for the money.
                "awaiting_payment": bool(sub and sub.gateway_subscription_id)
                and sub.status in (WorkspaceSubscription.Status.PENDING, WorkspaceSubscription.Status.PAST_DUE),
            }
        )

    paying = [r for r in rows if r["subscription"] and r["subscription"].status == WorkspaceSubscription.Status.ACTIVE]
    due_dates = [r["subscription"].current_period_end for r in paying if r["subscription"].current_period_end]
    # Only where the user runs the organization: the payer's data is the
    # organization's, not a brand manager's.
    payer = None
    if request.org_membership and request.org_membership.org_role in (
        OrgMembership.OrgRole.OWNER,
        OrgMembership.OrgRole.ADMIN,
    ):
        profile = getattr(request.org, "billing_profile", None)
        payer = {
            "organization": request.org,
            "name": (profile.payer_name if profile else "") or request.org.name,
            "tax_id_ending": profile.tax_id[-4:] if profile and profile.tax_id else "",
            "phone": _display_phone(profile.phone) if profile else "",
            "email": request.org.billing_email,
        }
    return render(
        request,
        "billing/account_billing.html",
        {
            "rows": rows,
            "counts": {
                "all": len(rows),
                "active": sum(1 for r in rows if r["state"] == "active"),
                "inactive": sum(1 for r in rows if r["state"] == "inactive"),
                "trial": sum(1 for r in rows if r["state"] == "trial"),
            },
            "plan": {
                "brands": len(paying),
                "next_due": min(due_dates) if due_dates else None,
                "recurring_cents": sum(r["monthly_total_cents"] for r in paying),
            },
            "payments": _payment_history([r["workspace"].id for r in rows]),
            "payer": payer,
            "settings_active": "billing",
        },
    )


@login_required
@require_org_role("admin")
@require_POST
def update_payer(request):
    """Change who the organization's invoices are issued to.

    Name, CPF or CNPJ, phone and billing email. The document field may be
    left empty to keep the one on file, which is never sent back to the
    page to be resubmitted.
    """
    name = (request.POST.get("payer_name") or "").strip()[:255]
    raw_tax_id = (request.POST.get("tax_id") or "").strip()
    raw_phone = (request.POST.get("phone") or "").strip()
    email = (request.POST.get("billing_email") or "").strip()

    tax_id = services.valid_tax_id(raw_tax_id)
    phone = services.valid_phone(raw_phone)
    error = None
    if not name:
        error = _("Enter the name or company name the invoices should carry.")
    elif raw_tax_id and not tax_id:
        error = _("Enter a valid CPF or CNPJ for the invoice.")
    elif raw_phone and not phone:
        error = _("Enter a phone with area code.")
    elif email:
        try:
            validate_email(email)
        except ValidationError:
            error = _("Enter a valid billing email.")
    if error:
        messages.error(request, error)
        return redirect("billing_account:overview")

    try:
        contracting.update_payer(request.org, name=name, tax_id=tax_id, phone=phone, email=email)
    except AsaasError as exc:
        logger.exception("Asaas refused the payer data of organization %s", request.org.pk)
        messages.error(
            request,
            _gateway_refusal(exc)
            or _("We could not reach the payment provider, so nothing was changed. Please try again in a moment."),
        )
        return redirect("billing_account:overview")

    messages.success(request, _("Billing data updated. The next invoices will carry it."))
    return redirect("billing_account:overview")


#: How many of the latest charges the account's billing page lists.
PAYMENT_HISTORY_LIMIT = 12


def _payment_history(workspace_ids):
    """The latest charges Asaas reported for these workspaces, newest first.

    Read from the webhook log, so it shows what the provider said happened
    and nothing we assumed. A charge sends several events along its life
    (confirmed, then received; overdue, then confirmed): only the newest
    event of each charge is kept, which is its current state.
    """
    events = (
        BillingEvent.objects.filter(
            Q(subscription__workspace_id__in=workspace_ids) | Q(addon__workspace_id__in=workspace_ids),
            status=BillingEvent.Status.PROCESSED,
            event_type__in=PAID_EVENTS | UNPAID_EVENTS,
        )
        .select_related("subscription__workspace", "addon__workspace")
        .order_by("-created_at")[: PAYMENT_HISTORY_LIMIT * 4]
    )
    seen = set()
    history = []
    for event in events:
        payment = event.payload.get("payment") if isinstance(event.payload, dict) else None
        payment_id = payment.get("id") if isinstance(payment, dict) else None
        if payment_id in seen:
            continue
        seen.add(payment_id)
        invoice_url = payment.get("invoiceUrl") if isinstance(payment, dict) else ""
        if event.event_type in PAID_EVENTS:
            outcome = "paid"
        elif event.event_type == "PAYMENT_OVERDUE":
            outcome = "overdue"
        else:
            outcome = "reversed"
        history.append(
            {
                "date": event.created_at,
                "workspace": event.subscription.workspace if event.subscription else event.addon.workspace,
                "is_addon": event.addon_id is not None,
                "amount_cents": _event_amount_cents(event.payload),
                "outcome": outcome,
                "invoice_url": invoice_url
                if isinstance(invoice_url, str) and invoice_url.startswith("https://")
                else "",
            }
        )
        if len(history) == PAYMENT_HISTORY_LIMIT:
            break
    return history


@login_required
@require_GET
def subscribe_modal(request, workspace_id):
    """The contracting modal shown when a workspace has no subscription.

    A page rather than markup injected into every layout: the only way in
    is the redirect right after creating a workspace (and the CTA on the
    billing page), and a page can be linked, reloaded and bookmarked
    after a failed attempt.
    """
    workspace = _get_workspace(request, workspace_id)
    if request.htmx:
        # The page asking whether the payment has been confirmed yet. It
        # leaves for the workspace as soon as the webhook has activated it.
        response = HttpResponse(status=204)
        if services.ensure_subscription(workspace).is_settled:
            response["HX-Redirect"] = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
        return response
    plan = services.plan_for(workspace.organization)
    if services.plan_has_room(plan):
        # The organization's plan has a brand left: this workspace takes it.
        services.sync_plan_coverage(plan, [workspace])
        if services.is_workspace_active(workspace.pk):
            return redirect("calendar:calendar", workspace_id=workspace.id)
    context = _billing_context(request, workspace)
    # Settled, not merely active: a workspace on its free trial is in use
    # and still has this page to open, to pay before the trial ends.
    if context["subscription"].is_settled:
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    if context["can_manage_billing"] and not context["awaiting_payment"]:
        # Contracting is the checkout. This page stays for an invoice of a
        # brand contracted on its own and for members who cannot pay.
        return redirect("billing:checkout", workspace_id=workspace.id)
    return render(request, "billing/subscribe_modal.html", context)


@login_required
@require_POST
def subscribe_confirm(request, workspace_id):
    """Contract the workspace's monthly subscription.

    The three outcomes are deliberately different to the user:

    - success          : contracted, awaiting the gateway's confirmation.
    - not configured   : an expected state in every environment without
                         Asaas credentials. Said plainly, as an ``info``
                         message, because nothing is broken and there is
                         nothing for the customer to retry.
    - gateway failure  : an error the customer may retry; logged with the
                         traceback so support has it, never shown one.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    tax_id, tax_id_error = _posted_tax_id(request, workspace)
    if tax_id_error:
        messages.error(request, tax_id_error)
        return redirect("billing:subscribe_modal", workspace_id=workspace.id)

    cycle = WorkspaceSubscription.Cycle.MONTHLY
    if request.POST.get("cycle") == WorkspaceSubscription.Cycle.YEARLY and services.annual_discount_percent() > 0:
        cycle = WorkspaceSubscription.Cycle.YEARLY

    try:
        contracting.subscribe_workspace(workspace, email=request.user.email, cpf_cnpj=tax_id, cycle=cycle)
    except AsaasNotConfigured:
        # Expected without credentials. The local row stays pending, so
        # pressing Contract again once Asaas is configured just works.
        logger.info("Workspace %s contracting attempted with Asaas unconfigured", workspace.pk)
        messages.info(
            request,
            _(
                "Billing is not set up on this installation yet, so no charge was created. "
                "Your workspace is registered and we will contact you to finish the subscription."
            ),
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    except AsaasError as exc:
        logger.exception("Asaas rejected contracting for workspace %s", workspace.pk)
        messages.error(
            request,
            _gateway_refusal(exc)
            or _("We could not reach the payment provider. Nothing was charged — please try again in a moment."),
        )
        return redirect("billing:subscribe_modal", workspace_id=workspace.id)

    messages.success(
        request,
        _("Subscription contracted. It activates as soon as the first payment is confirmed."),
    )
    # Back to the contracting page, which now offers the invoice and
    # waits for the payment to be confirmed.
    return redirect("billing:subscribe_modal", workspace_id=workspace.id)


@login_required
@require_POST
def start_trial(request, workspace_id):
    """Start the account's free trial on this workspace.

    Asks for the CPF or CNPJ and a phone before opening it: the trial is
    one per person, and those are what tell two accounts of the same
    person apart.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    # Asked in the checkout's billing details, which the trial goes through
    # without the payment, unless already on file.
    profile = services.ensure_billing_profile(workspace.organization)
    tax_id = services.valid_tax_id(request.POST.get("tax_id")) or profile.tax_id
    phone = services.valid_phone(request.POST.get("phone")) or profile.phone
    if not tax_id or not phone:
        return _checkout_error(
            request, _("Enter a valid CPF or CNPJ and a phone with area code to start the free trial.")
        )

    try:
        services.start_trial(workspace, tax_id=tax_id, phone=phone, email=request.user.email, user=request.user)
    except services.TrialRefusedError as exc:
        return _checkout_error(request, str(exc))

    # The brand renamed on its row and the organization named, as on payment.
    _apply_names(request, workspace.organization, _brand_rows(request, workspace.organization, None))
    return _checkout_done(request, workspace, trial=True)


@login_required
@require_GET
def pay_invoice(request, workspace_id):
    """Send the customer to the invoice of what this workspace owes."""
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    try:
        url = contracting.open_invoice_url(workspace)
    except AsaasError:
        logger.exception("Could not read the invoice of workspace %s", workspace.pk)
        messages.error(request, _("We could not reach the payment provider. Please try again in a moment."))
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    if not url:
        messages.info(
            request,
            _("There is no open invoice for this workspace. A payment already made can take a few minutes to show."),
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    return redirect(url)


# ---------------------------------------------------------------------------
# Checkout — the organization's brands, chosen and paid by card
# ---------------------------------------------------------------------------

#: Quick picks of the "Add brands" dialog.
ADD_BRAND_CHOICES = (1, 2, 3, 5, 10)


def _parse_cycle(raw):
    if raw == WorkspaceSubscription.Cycle.YEARLY and services.annual_discount_percent() > 0:
        return WorkspaceSubscription.Cycle.YEARLY
    return WorkspaceSubscription.Cycle.MONTHLY


def _checkout_coupon(code, organization):
    """``(coupon, error)`` for the code typed at checkout; both None when nothing was typed."""
    try:
        return services.find_coupon(code, organization), None
    except services.CouponError as exc:
        return None, str(exc)


def _active_plan(organization):
    plan = services.plan_for(organization)
    return plan if plan and plan.is_active else None


def _brand_rows(request, organization, plan):
    """Every brand of the organization as the checkout lists it, oldest first.

    ``state`` says what a row is: ``plan`` (paid on the plan, locked),
    ``courtesy`` or ``own`` (comped, or paying on a subscription of its
    own: locked and not charged here), ``trial`` or ``inactive`` (can be
    ticked). ``renamable`` when this member manages the brand.
    """
    subscriptions = {
        sub.workspace_id: sub
        for sub in WorkspaceSubscription.objects.filter(
            workspace__organization=organization, workspace__is_archived=False
        )
    }
    rows = []
    for workspace in Workspace.objects.filter(organization=organization, is_archived=False).order_by("created_at"):
        sub = subscriptions.get(workspace.pk) or services.ensure_subscription(workspace)
        if plan is not None and sub.plan_id == plan.pk:
            state = "plan"
        elif sub.is_courtesy:
            state = "courtesy"
        elif sub.gateway_subscription_id and sub.status != WorkspaceSubscription.Status.CANCELED:
            state = "own"
        elif sub.in_trial:
            state = "trial"
        else:
            state = "inactive"
        rows.append(
            {
                "workspace": workspace,
                "subscription": sub,
                "state": state,
                "selectable": state in ("trial", "inactive"),
                "renamable": _can_manage_billing(request, workspace),
            }
        )
    return rows


def _chosen(data, rows):
    """``(workspaces, new_names)`` picked on the checkout form.

    Only brands that can be ticked count, whatever ids the request carries;
    new brand names are cleaned and empty ones dropped.
    """
    wanted = set(data.getlist("workspaces"))
    workspaces = [row["workspace"] for row in rows if row["selectable"] and str(row["workspace"].pk) in wanted]
    names = [name for name in (clean_display_name(raw) for raw in data.getlist("new_brand")) if name]
    return workspaces, names[: services.PLAN_MAX_BRANDS]


def _checkout_brands(plan, workspaces, names):
    """How many brands the plan has after this checkout, or None past the limit."""
    total = (plan.brands if plan else 0) + len(workspaces) + len(names)
    return total if 0 < total <= services.PLAN_MAX_BRANDS else None


def _apply_names(request, organization, rows):
    """Rename the brands edited in place and keep what the billing details asked.

    The brand an account was created with is renamed on its row. The name
    the invoices carry (``payer_name``) and the billing details' extra
    question (clients served, or segment) are kept. An organization still
    with its sign-up name (it is named on the "about your business" step)
    takes the first brand's. Runs before the customer is created at Asaas,
    which takes the payer name.
    """
    for row in rows:
        if not row["renamable"]:
            continue
        name = clean_display_name(request.POST.get(f"name_{row['workspace'].pk}", ""))
        if name and name != row["workspace"].name:
            row["workspace"].name = name
            row["workspace"].save(update_fields=["name"])
    if not _can_manage_billing(request, rows[0]["workspace"]):
        return
    # Whose name the invoices carry, typed in the billing details.
    payer_name = clean_display_name(request.POST.get("payer_name", ""), max_length=255)
    if payer_name:
        profile = services.ensure_billing_profile(organization)
        if payer_name != profile.payer_name:
            profile.payer_name = payer_name
            profile.save(update_fields=["payer_name", "updated_at"])
    if organization.is_unnamed:
        name = clean_display_name(request.POST.get("organization_name", "")) or rows[0]["workspace"].name
        if name and name != Organization.DEFAULT_NAME:
            organization.name = name
            organization.save(update_fields=["name"])
    # The extra question of the billing details (_checkout_wording), kept
    # when the answer is one of its choices.
    for field, choices in (
        ("client_count", Organization.ClientCount.values),
        ("business_segment", Organization.Segment.values),
    ):
        answer = request.POST.get(field, "")
        if answer in choices and answer != getattr(organization, field):
            setattr(organization, field, answer)
            organization.save(update_fields=[field])


def _checkout_wording(organization):
    """The billing details' extra question, after the account's activity.

    The activity comes from the "about your business" step: how many clients
    an agency or a freelancer serves, or the segment of a business or of the
    company a marketing analyst works for. None without an activity.
    """
    clients = ("client_count", _("How many clients do you serve today?"), Organization.ClientCount.choices)
    questions = {
        Organization.Activity.AGENCY: clients,
        Organization.Activity.FREELANCER: clients,
        Organization.Activity.ENTREPRENEUR: (
            "business_segment",
            _("What is your business's segment?"),
            Organization.Segment.choices,
        ),
        Organization.Activity.MARKETING_ANALYST: (
            "business_segment",
            _("What is the company's segment?"),
            Organization.Segment.choices,
        ),
    }
    question = questions.get(organization.business_activity)
    if question is None:
        return {"extra": None}
    field, label, choices = question
    return {"extra": {"field": field, "label": label, "choices": choices, "value": getattr(organization, field)}}


def _included_networks() -> list[tuple[str, str]]:
    """The networks a brand can connect, as (icon key, name), once each.

    Read from the platforms an admin left visible, so the checkout never
    sells a network that is switched off. Routes to the same network
    (Instagram Login, LinkedIn profile and page) are shown once.
    """
    same = {**PlatformVisibility.SAME_NETWORK, "linkedin_personal": "linkedin", "linkedin_company": "linkedin"}
    names = {"linkedin": "LinkedIn"}
    networks: dict[str, str] = {}
    for value, label in PlatformVisibility.visible_choices():
        key = same.get(value, value)
        networks.setdefault(key, names.get(key, str(label)))
    return list(networks.items())


@login_required
@require_GET
def checkout(request, workspace_id):
    """The organization's plan: which brands, monthly or yearly, and the card.

    One page in two steps. First the brands of the organization, each with
    a tick, plus new ones added as rows, and the cycle; a bar at the foot
    keeps the total. Then the payment beside the order summary. Without an
    active plan the card is charged now and kept for every renewal; with
    one, the brands ticked are added to it and charged to the card on file
    from the next charge. Also where a new account starts, once asked about
    its business: it names its brand on its row and can start the free
    trial instead.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)
    if needs_about(request, workspace):
        return redirect(f"{reverse('organizations:about')}?workspace={workspace.id}")
    organization = workspace.organization
    plan = services.plan_for(organization)
    if not services.is_workspace_active(workspace.pk) and services.plan_has_room(plan):
        services.sync_plan_coverage(plan, [workspace])
        if services.is_workspace_active(workspace.pk):
            return redirect("calendar:calendar", workspace_id=workspace.id)

    had_plan = plan is not None
    plan = _active_plan(organization)
    rows = _brand_rows(request, organization, plan)
    for row in rows:
        # Ticked from the start: the brand being opened, and every brand on trial or off.
        row["checked"] = row["selectable"] and (row["workspace"].pk == workspace.pk or not plan)
    workspaces = [row["workspace"] for row in rows if row.get("checked")]
    # Yearly unless the link asks for monthly: the cheaper choice is the one shown first.
    cycle = plan.billing_cycle if plan else _parse_cycle(request.GET.get("cycle") or WorkspaceSubscription.Cycle.YEARLY)
    brands = _checkout_brands(plan, workspaces, []) or 1
    profile = services.ensure_billing_profile(organization)
    # The "Include" ticks are for an organization that has had a plan, or
    # that has several brands to choose from. A first purchase of the one
    # brand an account starts with has nothing to choose: it is included.
    choose_brands = had_plan or sum(row["selectable"] for row in rows) > 1
    subscription = services.ensure_subscription(workspace)
    context = {
        "workspace": workspace,
        "organization": organization,
        "plan": plan,
        "rows": rows,
        "choose_brands": choose_brands,
        "cycle": cycle,
        "annual_percent": services.annual_discount_percent(),
        "volume_tiers": services.volume_discounts(),
        "unit": {
            "monthly": services.quote_plan(1, WorkspaceSubscription.Cycle.MONTHLY)["monthly_cents"],
            "yearly": services.quote_plan(1, WorkspaceSubscription.Cycle.YEARLY)["monthly_cents"],
        },
        "quote": services.quote_plan(brands, cycle, plan.coupon if plan else None),
        "add_choices": ADD_BRAND_CHOICES,
        "max_brands": services.PLAN_MAX_BRANDS,
        "payer_tax_id": profile.tax_id,
        "payer_phone": profile.phone,
        "payer_name": profile.payer_name,
        "in_setup": needs_welcome(request, workspace),
        "wording": _checkout_wording(organization),
        "trial_available": not plan and services.trial_available(workspace, request.user),
        "trial_days": services.trial_days(),
        "trial_expired": subscription.trial_expired,
        "subscription_required": services.subscription_required(),
        "gateway_configured": _gateway_configured(),
        "renews_on": services.renewal_date(cycle),
        "networks": _included_networks(),
        "yearly_quote": services.quote_plan(brands, WorkspaceSubscription.Cycle.YEARLY)
        if not plan and cycle == WorkspaceSubscription.Cycle.MONTHLY
        else None,
    }
    return render(request, "billing/checkout.html", context)


@login_required
@require_GET
def checkout_quote(request, workspace_id):
    """HTMX fragment: the total of the brands ticked and the cycle, priced by the server.

    The foot bar, plus the saving under the brands and the order summary of the payment step out of band.
    """
    workspace = _get_workspace(request, workspace_id)
    organization = workspace.organization
    plan = _active_plan(organization)
    rows = _brand_rows(request, organization, plan)
    workspaces, names = _chosen(request.GET, rows)
    brands = _checkout_brands(plan, workspaces, names)
    cycle = plan.billing_cycle if plan else _parse_cycle(request.GET.get("cycle"))
    coupon, coupon_error = (plan.coupon, None) if plan else _checkout_coupon(request.GET.get("coupon"), organization)
    context = {
        "workspace": workspace,
        "plan": plan,
        "quote": services.quote_plan(brands, cycle, coupon) if brands else None,
        "chosen_count": len(workspaces) + len(names),
        "coupon_error": coupon_error,
        "max_brands": services.PLAN_MAX_BRANDS,
        "renews_on": services.renewal_date(cycle),
        "annual_percent": services.annual_discount_percent(),
        "yearly_quote": services.quote_plan(brands, WorkspaceSubscription.Cycle.YEARLY, coupon)
        if brands and not plan and cycle == WorkspaceSubscription.Cycle.MONTHLY
        else None,
    }
    return render(request, "billing/_checkout_quote.html", context)


def _card_number_is_valid(number: str) -> bool:
    """The Luhn check digit every real card number carries.

    Asaas answers a number that fails it with HTTP 500 instead of a refusal,
    which leaves the checkout unsure whether anything was charged; checking
    it first turns a mistyped number into a plain field error.
    """
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _parse_card(post):
    """The card and the address the gateway asks for, from the checkout form.

    Returns ``(card, address, error)``. Shape only; the gateway decides
    whether the card is good. The values are passed on and kept nowhere.
    """
    number = re.sub(r"\D", "", post.get("card_number", ""))
    expiry = re.sub(r"\D", "", post.get("card_expiry", ""))
    ccv = re.sub(r"\D", "", post.get("card_ccv", ""))
    holder_name = " ".join(post.get("card_holder", "").split())[:100]
    postal_code = re.sub(r"\D", "", post.get("postal_code", ""))
    address_number = post.get("address_number", "").strip()[:10]
    if not 13 <= len(number) <= 19:
        return None, None, _("Enter the card number.")
    if not _card_number_is_valid(number):
        return None, None, _("This card number is not valid. Check the digits.")
    if len(expiry) == 4:
        expiry = expiry[:2] + "20" + expiry[2:]
    if len(expiry) != 6 or not 1 <= int(expiry[:2]) <= 12:
        return None, None, _("Enter the card's expiry as MM/YY.")
    today = timezone.localdate()
    if (int(expiry[2:]), int(expiry[:2])) < (today.year, today.month):
        return None, None, _("This card has expired.")
    if not 3 <= len(ccv) <= 4:
        return None, None, _("Enter the card's security code.")
    if not holder_name:
        return None, None, _("Enter the name printed on the card.")
    if len(postal_code) != 8 or not address_number:
        return None, None, _("Enter the CEP and the number of the card's billing address.")
    card = {
        "holderName": holder_name,
        "number": number,
        "expiryMonth": expiry[:2],
        "expiryYear": expiry[2:],
        "ccv": ccv,
    }
    return card, {"postalCode": postal_code, "addressNumber": address_number, "name": holder_name}, None


def _checkout_error(request, message, *, status=422):
    """A refusal of the checkout form, shown above its button without a reload.

    The form posts through HTMX so what was typed (the card included) stays
    on the page; a plain post would lose it to the redirect. The text goes
    to the form's ``data-inline-error`` container (base.html's htmx error
    listener).
    """
    if request.htmx:
        return HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")
    messages.error(request, message)
    return redirect(request.META.get("HTTP_REFERER") or "dashboard")


def _checkout_done(request, workspace, *, trial=False):
    """On to the welcome page, which says what is open now and where to start."""
    target = reverse("billing:checkout_welcome", kwargs={"workspace_id": workspace.id})
    if trial:
        target += "?trial=1"
    if request.htmx:
        return _hx_redirect(target)
    return redirect(target)


def _checkout_order(request, organization, *, card):
    """What the checkout's payment step asks for, checked before anything reaches Asaas.

    The brands ticked and added, the coupon, the CPF or CNPJ and phone (kept
    when they changed) and, when ``card`` is set, the card. Returns
    ``(order, None)`` with the brands, the new brand names, the coupon, the
    document, the phone and the parsed card and address, or ``(None, error
    response)``. The names and the billing details' answers are applied
    once the order is valid.
    """
    rows = _brand_rows(request, organization, None)
    workspaces, names = _chosen(request.POST, rows)
    brands = _checkout_brands(None, workspaces, names)
    if brands is None:
        return None, _checkout_error(
            request, _("Choose between 1 and %(max)s brands.") % {"max": services.PLAN_MAX_BRANDS}
        )
    coupon, coupon_error = _checkout_coupon(request.POST.get("coupon"), organization)
    if coupon_error:
        return None, _checkout_error(request, coupon_error)
    profile = services.ensure_billing_profile(organization)
    # What was typed now wins over what is on file: a retry after Asaas
    # refused the phone or the document must send the corrected one.
    tax_id = services.valid_tax_id(request.POST.get("tax_id")) or profile.tax_id
    phone = services.valid_phone(request.POST.get("phone")) or profile.phone
    if not tax_id or not phone:
        return None, _checkout_error(request, _("Enter a valid CPF or CNPJ and a phone with area code."))
    card_data, address = None, None
    if card:
        card_data, address, error = _parse_card(request.POST)
        if error:
            return None, _checkout_error(request, error)
    if (tax_id, phone) != (profile.tax_id, profile.phone):
        services.set_payer_identity(organization, tax_id=tax_id, phone=phone)
    _apply_names(request, organization, rows)
    return {
        "brands": brands,
        "workspaces": workspaces,
        "names": names,
        "coupon": coupon,
        "tax_id": tax_id,
        "phone": phone,
        "card": card_data,
        "address": address,
    }, None


@login_required
@require_POST
@ratelimit(key="user", rate="8/h", method="POST", block=False)
def checkout_pay(request, workspace_id):
    """Buy the brands ticked and added, and pay the first charge with the card sent."""
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)
    organization = workspace.organization
    if getattr(request, "limited", False):
        # Against card testing: a handful of tries an hour is plenty for a real customer.
        return _checkout_error(request, _("Too many payment attempts. Please wait a while before trying again."))
    if _active_plan(organization):
        # Already paid for: brands are added to it, never bought twice.
        return checkout_add_brands(request, workspace_id)

    order, error = _checkout_order(request, organization, card=True)
    if error:
        return error
    brands, workspaces, names, coupon = order["brands"], order["workspaces"], order["names"], order["coupon"]
    tax_id, phone, card, address = order["tax_id"], order["phone"], order["card"], order["address"]

    email = organization.billing_email or request.user.email
    holder = {**address, "email": email, "cpfCnpj": tax_id, "phone": phone}
    try:
        contracting.purchase_plan(
            organization,
            brands=brands,
            cycle=_parse_cycle(request.POST.get("cycle")),
            card=card,
            holder=holder,
            remote_ip=client_ip(request) or "",
            email=email,
            coupon=coupon,
            workspaces=workspaces,
            new_brand_names=names,
            owner=request.user,
        )
    except contracting.CardRefusedError as exc:
        return _checkout_error(request, str(exc))
    except contracting.CardChallengeRequiredError as exc:
        # The issuer's own authentication page; the webhook activates the
        # plan once the payment is confirmed there.
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = exc.url
            return response
        return redirect(exc.url)
    except AsaasNotConfigured:
        return _checkout_error(request, _("Payments are not connected on this installation yet. Nothing was charged."))
    except AsaasError as exc:
        logger.exception("Checkout failed for organization %s", organization.pk)
        return _checkout_error(
            request,
            _gateway_refusal(exc)
            or _(
                "We could not confirm the payment with the provider. If the card was charged, "
                "your brands open in a few minutes; otherwise, try again."
            ),
        )
    return _checkout_done(request, workspace)


@login_required
@require_POST
@ratelimit(key="user", rate="8/h", method="POST", block=False)
def checkout_pix(request, workspace_id):
    """Buy the brands ticked and added by Pix Automático: answer the QR code to pay.

    The QR code pays the first charge and authorizes the renewals; the page
    then waits for the payment (``checkout_status``). A new QR code
    withdraws the one before it.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)
    organization = workspace.organization
    if getattr(request, "limited", False):
        return _checkout_error(request, _("Too many payment attempts. Please wait a while before trying again."))
    if _active_plan(organization):
        return _checkout_done(request, workspace)

    order, error = _checkout_order(request, organization, card=False)
    if error:
        return error
    try:
        plan, qr_code = contracting.start_pix_plan(
            organization,
            brands=order["brands"],
            cycle=_parse_cycle(request.POST.get("cycle")),
            email=organization.billing_email or request.user.email,
            coupon=order["coupon"],
            workspaces=order["workspaces"],
            new_brand_names=order["names"],
            owner=request.user,
        )
    except AsaasNotConfigured:
        return _checkout_error(request, _("Payments are not connected on this installation yet. Nothing was charged."))
    except AsaasError as exc:
        logger.exception("Pix checkout failed for organization %s", organization.pk)
        return _checkout_error(
            request,
            _gateway_refusal(exc) or _("We could not create the Pix QR code with the provider. Please try again."),
        )
    if qr_code is None:
        # An earlier QR code had been paid after all.
        return _checkout_done(request, workspace)
    qr_code["expires_in"] = max(int((qr_code["expires_at"] - timezone.now()).total_seconds()), 0)
    return render(
        request,
        "billing/_pix_qr_code.html",
        {"workspace": workspace, "plan": plan, "qr_code": qr_code},
    )


@login_required
@require_GET
def checkout_status(request, workspace_id):
    """HTMX poll of the Pix QR code: on to the welcome page once the plan is paid, else nothing."""
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)
    if _active_plan(workspace.organization):
        return _checkout_done(request, workspace)
    return HttpResponse(status=204)


@login_required
@require_POST
def checkout_add_brands(request, workspace_id):
    """Add the brands ticked and created to the organization's active plan."""
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)
    organization = workspace.organization
    plan = _active_plan(organization)
    if plan is None:
        return redirect("billing:checkout", workspace_id=workspace.id)
    rows = _brand_rows(request, organization, plan)
    workspaces, names = _chosen(request.POST, rows)
    brands = _checkout_brands(plan, workspaces, names)
    if brands is None or brands == plan.brands:
        return _checkout_error(request, _("Tick or add the brands to put on the plan."))
    _apply_names(request, organization, rows)
    try:
        contracting.change_plan_brands(plan, brands, workspaces=workspaces, new_brand_names=names, owner=request.user)
    except AsaasError as exc:
        logger.exception("Could not add brands to plan %s", plan.pk)
        return _checkout_error(
            request, _gateway_refusal(exc) or _("We could not reach the payment provider. Please try again.")
        )
    messages.success(request, _("Brands added to your plan. The new value applies from the next charge."))
    target = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    return _hx_redirect(target) if request.htmx else redirect(target)


def _hx_redirect(url):
    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return response


@login_required
@require_GET
def checkout_welcome(request, workspace_id):
    """Right after paying or starting the trial: what is open now and where to start.

    Three steps, each a link into the workspace: connect the networks, create
    in the idea hub, then automate replies on the posts.
    """
    workspace = _get_workspace(request, workspace_id)
    return render(
        request,
        "billing/checkout_welcome.html",
        {
            "workspace": workspace,
            "trial": request.GET.get("trial") == "1",
            "subscription": services.ensure_subscription(workspace),
        },
    )


@login_required
@require_GET
def storage_quote(request, workspace_id):
    """HTMX fragment: the price of the quantity currently selected.

    Server-side so the number the customer agrees to is the number the
    server computed — an Alpine-only calculation would let a tampered
    page display one price and buy at another.
    """
    workspace = _get_workspace(request, workspace_id)
    quantity = _parse_quantity_gb(request.GET.get("quantity_gb"))
    context = {
        "workspace": workspace,
        "quote": _storage_quote(quantity) if quantity else None,
        "invalid": quantity is None and bool(request.GET.get("quantity_gb")),
        "min_gb": STORAGE_ADDON_MIN_GB,
        "max_gb": STORAGE_ADDON_MAX_GB,
    }
    return render(request, "billing/_storage_quote.html", context)


@login_required
@require_POST
def contract_storage(request, workspace_id):
    """Buy extra GB for this workspace.

    The add-on row is written before the gateway is called so a failed or
    unconfigured gateway leaves a ``pending`` record of what the customer
    asked for — which grants no disk (only ``active`` add-ons count) and
    can be retried or cleaned up, instead of vanishing.
    """
    workspace = _get_workspace(request, workspace_id)
    _require_manage_billing(request, workspace)

    quantity = _parse_quantity_gb(request.POST.get("quantity_gb"))
    if quantity is None:
        messages.error(
            request,
            _("Choose how much extra storage to contract, between %(min)s GB and %(max)s GB.")
            % {"min": STORAGE_ADDON_MIN_GB, "max": STORAGE_ADDON_MAX_GB},
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    # Checked before the add-on row is written, so a mistyped document
    # does not leave a pending request behind on every attempt.
    tax_id, tax_id_error = _posted_tax_id(request, workspace)
    if tax_id_error:
        messages.error(request, tax_id_error)
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    addon = services.contract_storage_addon(workspace, quantity)

    try:
        contracting.subscribe_storage_addon(addon, cpf_cnpj=tax_id)
    except AsaasNotConfigured:
        logger.info("Storage add-on %s created with Asaas unconfigured", addon.pk)
        messages.info(
            request,
            _(
                "Billing is not set up on this installation yet, so no charge was created. "
                "Your request for %(gb)s GB was recorded and we will contact you to finish it."
            )
            % {"gb": quantity},
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)
    except AsaasError as exc:
        logger.exception("Asaas rejected storage add-on %s", addon.pk)
        messages.error(
            request,
            _gateway_refusal(exc)
            or _("We could not reach the payment provider. Nothing was charged — please try again in a moment."),
        )
        return redirect("billing:workspace_billing", workspace_id=workspace.id)

    messages.success(
        request,
        _("%(gb)s GB contracted. The space is released as soon as the payment is confirmed.") % {"gb": quantity},
    )
    return redirect("billing:workspace_billing", workspace_id=workspace.id)


# ---------------------------------------------------------------------------
# Administrative area (operator only)
# ---------------------------------------------------------------------------
# Four screens, split by the question each one answers, instead of the
# single page this used to be:
#
# - Customers      : "who is paying, who is comped, who is out of disk" —
#                    the comparative view, and the only screen with
#                    actions on it (courtesy, quota, impersonation).
# - Plan and prices: "what are we charging" — the configured list prices
#                    and included disk, which until now existed only in
#                    settings and as a subtitle on the console.
# - Access log     : "who was inside a customer's account, and when" —
#                    ``ImpersonationSession`` has had a model and an
#                    index for exactly this question since impersonation
#                    shipped, and no screen to ask it from.
#
# - Subscriptions  : "what does each individual contract look like, and
#                    what has the provider actually told us about money" —
#                    the per-row view the two screens above cannot give,
#                    one being per account and the other per figure.
#
# The split is by *question*, not by entity. Mixing the price list into
# the customer list, which is what the old subtitle did, meant the product
# owner could not see it without loading every organization's billing
# rows.
#
# Plan and prices is editable as of ``staff_update_prices``. It used to
# say it could not be, on the grounds that list prices lived in settings
# and every sold row snapshots its own price — the second half of which is
# precisely why editing is safe, and is now the thing the screen is
# careful to say. The first half is what ``billing.ProductPrice`` fixes.


def _is_billing_staff(request):
    """Gate for the cross-organization billing console.

    ``is_staff``, not ``require_org_permission``: every org-scoped
    decorator in this codebase resolves an ``OrgMembership`` for the
    ``org_id`` in the URL, and this console deliberately has no org in
    its URL — it lists *every* organization so the product owner can comp
    one. There is no membership that could grant that, and inventing an
    org permission for it would mean any customer who grants themselves
    that permission could read every other customer's billing.

    ``is_staff`` is the account flag Django already reserves for
    operators, is what ``/admin/`` uses, and is not something a customer
    can set on themselves through the product.

    Asked via ``impersonation.is_operator`` rather than reading
    ``request.user.is_staff`` directly, because the two answers differ in
    one case that matters: while an operator is impersonating a customer
    for support, this console must be closed. Letting it through would
    mean the session acting as the customer everywhere else could comp a
    workspace or read every other account's billing, and the audit trail
    would record the customer's identity doing it.
    """
    return impersonation.is_operator(request)


def _staff_required(view_func):
    """``login_required`` plus the staff check, 403 for logged-in non-staff.

    Deliberately not ``user_passes_test``: that redirects to the login
    page on failure, so an already-authenticated member who found the URL
    would bounce between login and here forever. Raising
    ``PermissionDenied`` matches how every other permission failure in
    this codebase ends, while the outer ``login_required`` still sends an
    anonymous visitor to log in.
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_billing_staff(request):
            raise PermissionDenied("Staff access required.")
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


@_staff_required
@require_GET
def staff_console(request):
    """Customers: every account, its brands and the people in each brand.

    One group of rows per **organization** (the account is what gets
    charged, exempted and edited): the account's line with its owner's
    e-mail, status, brands, disk and monthly charge; unfolded, each brand
    with its own status, disk and charge, and under it the users linked to
    that brand with their role there. A user linked to several brands shows
    under each of them. Members of the organization linked to no brand are
    listed in a group of their own so nobody drops off the screen.

    ``people`` is each account's distinct users, for the per-person details
    and deletion panels. ``may_impersonate`` and ``may_deactivate`` are
    decided here by the same rules the POSTs enforce, so the page never
    offers an action that would answer 403 — notably not on another
    operator's account.
    """
    organizations = (
        Organization.objects.all()
        .prefetch_related(
            Prefetch(
                "workspaces",
                queryset=Workspace.objects.select_related("billing_subscription")
                .prefetch_related(
                    Prefetch(
                        "memberships",
                        queryset=WorkspaceMembership.objects.select_related("user").order_by("user__email"),
                    )
                )
                .order_by("name"),
            ),
            Prefetch(
                "memberships",
                queryset=OrgMembership.objects.select_related("user").order_by("user__email"),
            ),
        )
        .order_by("name")
    )

    operator = impersonation.real_user(request)
    flags: dict = {}

    def person(user, role, org_role=""):
        if user.pk not in flags:
            flags[user.pk] = (impersonation.may_impersonate(operator, user), _may_deactivate(operator, user))
        may_impersonate, may_deactivate = flags[user.pk]
        return {
            "user": user,
            "role": role,
            "org_role": org_role,
            "may_impersonate": may_impersonate,
            "may_deactivate": may_deactivate,
        }

    rows = []
    for organization in organizations:
        profile = services.ensure_billing_profile(organization)
        org_roles = {m.user_id: m.org_role for m in organization.memberships.all()}
        people: dict = {}
        workspace_rows = []
        for workspace in organization.workspaces.all():
            users = []
            for membership in workspace.memberships.all():
                entry = person(membership.user, membership.workspace_role, org_roles.get(membership.user_id, ""))
                users.append(entry)
                people.setdefault(membership.user_id, {**entry, "brands": []})["brands"].append(
                    (workspace.name, membership.workspace_role)
                )
            workspace_rows.append(
                {
                    "workspace": workspace,
                    "subscription": services.ensure_subscription(workspace),
                    "storage": storage_status(workspace),
                    "monthly_total_cents": services.workspace_monthly_total_cents(workspace),
                    "users": users,
                }
            )
        without_brand = [
            person(m.user, m.org_role, m.org_role) for m in organization.memberships.all() if m.user_id not in people
        ]
        for entry in without_brand:
            people[entry["user"].pk] = {**entry, "brands": []}
        for user_id, entry in people.items():
            entry["org_role"] = org_roles.get(user_id, "")
        owner_membership = next(
            (m for m in organization.memberships.all() if m.org_role == OrgMembership.OrgRole.OWNER),
            next(iter(organization.memberships.all()), None),
        )
        owner = people.get(owner_membership.user_id) if owner_membership else None
        rows.append(
            {
                "organization": organization,
                "profile": profile,
                "workspaces": workspace_rows,
                "workspace_count": len(workspace_rows),
                "monthly_total_cents": sum(w["monthly_total_cents"] for w in workspace_rows),
                "courtesy_exceeded": services.courtesy_quota_exceeded(organization),
                "owner": owner,
                "people": list(people.values()),
                "without_brand": without_brand,
                "is_team_account": bool(owner and owner["user"].is_staff),
                "status": _account_status(workspace_rows),
                "storage_used_bytes": sum(w["storage"].used_bytes for w in workspace_rows),
                "storage_limit_bytes": sum(w["storage"].limit_bytes for w in workspace_rows),
                "storage_percent": _storage_percent(workspace_rows),
            }
        )

    return render(
        request,
        "billing/staff_console.html",
        {
            "rows": rows,
            "workspace_price_cents": services.workspace_price_cents(),
            "storage_price_per_gb_cents": services.storage_price_per_gb_cents(),
            "admin_active": "accounts",
        },
    )


# Order the account-level status cell resolves in: the most urgent state
# among the account's workspaces wins, because the operator scanning the
# column is looking for the account that needs attention, not for a
# representative sample. Past due beats everything (someone's charge
# failed); pending beats active (never contracted); courtesy is reported
# last because it is a decision, not a problem.
_STATUS_PRIORITY = (
    WorkspaceSubscription.Status.PAST_DUE,
    WorkspaceSubscription.Status.PENDING,
    WorkspaceSubscription.Status.CANCELED,
    WorkspaceSubscription.Status.ACTIVE,
    WorkspaceSubscription.Status.COURTESY,
)


def _account_status(workspace_rows):
    """The one status to show for a whole account, or "" when it has none.

    Empty string rather than a fake status for an account with no
    workspace: there is nothing to be in a state about, and inventing
    "pending" would put an account on the operator's chase list for not
    having done anything yet.
    """
    present = {row["subscription"].status for row in workspace_rows}
    for status in _STATUS_PRIORITY:
        if status in present:
            return status
    return ""


def _storage_percent(workspace_rows):
    """Disk used across an account's workspaces, as a percentage.

    Summed then divided, not an average of percentages: one workspace at
    100% and one empty is an account at 50% of what it bought, and
    averaging the two would say the same thing by accident while
    disagreeing the moment the quotas differ.
    """
    limit = sum(row["storage"].limit_bytes for row in workspace_rows)
    if limit <= 0:
        return 0.0
    used = sum(row["storage"].used_bytes for row in workspace_rows)
    return (used / limit) * 100


def _may_deactivate(operator, target):
    """Whether ``operator`` may switch ``target``'s access off.

    The same two refusals ``may_impersonate`` makes, and for the same
    reasons: an operator must not lock themselves out of the only area
    they can use, and must not be able to disable a colleague — that is
    privilege escalation dressed up as support, and it would leave the
    product with no reachable operator at all if aimed at the last one.

    Deliberately NOT ``may_impersonate`` itself: that one also refuses an
    already-inactive target, and reactivating one is exactly what this
    permits. Expressed here rather than inline in the view so the page
    and the POST read the identical rule.
    """
    if operator is None or not operator.is_authenticated:
        return False
    if not (operator.is_active and operator.is_staff):
        return False
    if target is None or target.pk == operator.pk:
        return False
    return not target.is_staff


@_staff_required
@require_GET
def staff_plans(request):
    """The plan: what a workspace costs, what it includes, and a form to
    change it.

    Editable as of this change, which is a reversal of what this screen
    said before — and the reversal is safe for exactly the reason the old
    docstring gave for refusing: every sold ``WorkspaceSubscription`` and
    ``WorkspaceAddon`` snapshots the price it was sold at. So an edit here
    moves the *list* price and nothing else, which is what the product
    owner wants (he has not even set the per-GB price yet) and is the one
    behaviour the form must be honest about on screen.

    The figures now come from ``services.price_list()`` — the database row
    when one exists, the ``BILLING_*`` setting when none does — read
    through the same three functions the customer-facing page reads, so
    the operator cannot be shown a price nobody is charged.

    What is subscribed is counted on the Subscriptions screen, beside the
    contracts it counts, rather than here: this screen answers what the
    product charges.
    """
    prices = services.price_list()

    return render(
        request,
        "billing/staff_plans.html",
        {
            "workspace_price_cents": services.workspace_price_cents(),
            "storage_price_per_gb_cents": services.storage_price_per_gb_cents(),
            "base_storage_bytes": services.base_storage_bytes(),
            # The editable form's own view of the same three figures,
            # each already in the unit its field asks for: reais for the
            # money ones, whole GB for disk. Converted here rather than
            # in the template — a template that divides by 100 is how a
            # price gets rendered as "50.0".
            #
            # Named for the unit the operator sees, not the unit stored,
            # so a field cannot be wired to the wrong one by accident.
            "prices": prices,
            "price_form": {
                "workspace_price_brl": services.price_input_value(
                    ProductPrice.Key.WORKSPACE_PRICE_CENTS,
                    prices[ProductPrice.Key.WORKSPACE_PRICE_CENTS]["value"],
                ),
                "base_storage_gb": services.price_input_value(
                    ProductPrice.Key.BASE_STORAGE_BYTES,
                    prices[ProductPrice.Key.BASE_STORAGE_BYTES]["value"],
                ),
                "storage_price_per_gb_brl": services.price_input_value(
                    ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS,
                    prices[ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS]["value"],
                ),
            },
            "rules": services.billing_rules(),
            "price_changes": services.recent_price_changes(),
            "storage_quota_enabled": getattr(settings, "BILLING_STORAGE_QUOTA_ENABLED", False),
            # Expected states, not an incident — see the module docstring.
            "gateway_configured": bool(getattr(settings, "ASAAS_API_KEY", "")),
            "admin_active": "plans",
        },
    )


# The three fields the form posts, each paired with the ``ProductPrice``
# key it writes. A tuple rather than three named reads so a field added
# later cannot be validated but not saved, or saved but not audited.
#
# Every name states the unit the OPERATOR types, which is not the unit
# stored: ``_brl`` fields take reais ("R$ 50,00", "50", "1.234,56") and
# ``_gb`` takes whole gigabytes. ``services.parse_price_value`` converts
# to the stored cents and bytes. Naming them for the typed unit is what
# keeps a field from being wired to the wrong converter — and asking an
# operator for cents, which the first version of this screen did, exposes
# the storage layer to somebody who thinks in reais.
_PRICE_FIELDS = (
    ("workspace_price_brl", ProductPrice.Key.WORKSPACE_PRICE_CENTS),
    ("base_storage_gb", ProductPrice.Key.BASE_STORAGE_BYTES),
    ("storage_price_per_gb_brl", ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS),
)


@_staff_required
@require_GET
def staff_coupons(request):
    """The coupons customers type at checkout: create one, see how much each was used, switch one off."""
    coupons = Coupon.objects.annotate(used=Count("redemptions")).select_related("created_by")
    return render(
        request,
        "billing/staff_coupons.html",
        {
            "coupons": coupons,
            "kinds": Coupon.Kind.choices,
            "durations": Coupon.Duration.choices,
            "admin_active": "coupons",
        },
    )


@_staff_required
@require_POST
def staff_create_coupon(request):
    try:
        coupon = services.create_coupon(request.POST, created_by=request.user)
    except services.CouponError as exc:
        messages.error(request, str(exc))
        return redirect("billing_staff:coupons")
    logger.info("Operator %s created coupon %s", request.user.email, coupon.code)
    messages.success(request, _("Coupon %(code)s created.") % {"code": coupon.code})
    return redirect("billing_staff:coupons")


@_staff_required
@require_POST
def staff_toggle_coupon(request, coupon_id):
    """Switch a coupon off or back on. Plans already paid with it keep their price."""
    coupon = get_object_or_404(Coupon, pk=coupon_id)
    coupon.is_active = not coupon.is_active
    coupon.save(update_fields=["is_active"])
    if coupon.is_active:
        messages.success(request, _("Coupon %(code)s is on again.") % {"code": coupon.code})
    else:
        messages.info(
            request, _("Coupon %(code)s is off. It can no longer be used at checkout.") % {"code": coupon.code}
        )
    return redirect("billing_staff:coupons")


@_staff_required
@require_POST
def staff_update_rules(request):
    """Save the billing rules: yearly discount, free trial, volume discounts and the subscription lock.

    All or nothing like the prices: every field is validated first
    (``services.parse_billing_rules``) and written only if all pass, each into
    the same ProductPrice row and trail. A change applies to what is
    contracted from then on; turning the lock on takes effect on the next
    page a customer opens.
    """
    try:
        values = services.parse_billing_rules(request.POST)
    except services.PriceValidationError as exc:
        messages.error(request, str(exc))
        return redirect("billing_staff:plans")

    changed = [key for key, value in values.items() if services.set_price(key, value, changed_by=request.user)[1]]
    if not changed:
        messages.info(request, _("The rules were already those values, so nothing changed."))
        return redirect("billing_staff:plans")

    logger.warning("Operator %s changed billing rules: %s", request.user.email, ", ".join(str(key) for key in changed))
    messages.success(request, _("Rules updated. Discounts and the free trial apply to what is contracted from now on."))
    return redirect("billing_staff:plans")


@_staff_required
@require_POST
def staff_update_prices(request):
    """Change the product's list prices.

    Validate-all-then-write, deliberately: the three figures are one
    decision ("this is the plan"), and writing the two that parsed while
    refusing the third would leave the price list in a state the operator
    never chose — a workspace price raised and the disk it includes still
    at the old figure. So a single bad field refuses the whole submission
    and nothing is written.

    What this endpoint can and cannot reach is the point of the whole
    feature: it writes ``ProductPrice`` rows, which are read only when
    something is *contracted*. Every subscription and add-on already sold
    carries its own price column and is not touched here — not by this
    view, not by ``services.set_price``, and not by any UPDATE either one
    runs. A customer's bill can only change by contracting something new.

    Behind ``_staff_required`` like every other write in this area, which
    also closes it during an impersonated session: a price changed from
    inside a customer's session would be recorded against the customer's
    identity in the trail, which is worse than no trail.
    """
    parsed = []
    for field, key in _PRICE_FIELDS:
        try:
            parsed.append((key, services.parse_price_value(key, request.POST.get(field))))
        except services.PriceValidationError as exc:
            messages.error(request, str(exc))
            # Nothing written — see the docstring. The operator's other
            # two values are still in the form they posted from.
            return redirect("billing_staff:plans")

    changed = []
    for key, value in parsed:
        _price, change = services.set_price(key, value, changed_by=request.user)
        if change is not None:
            changed.append(key)

    if not changed:
        messages.info(request, _("The prices were already those values, so nothing changed."))
        return redirect("billing_staff:plans")

    # Logged as well as recorded in ProductPriceChange: the trail is the
    # record of truth, and the log line is what correlates a price move
    # with whatever else happened in that deploy's logs.
    logger.warning(
        "Operator %s changed %s list price(s): %s",
        request.user.email,
        len(changed),
        ", ".join(str(key) for key in changed),
    )
    messages.success(
        request,
        _(
            "Prices updated. Only subscriptions and add-ons contracted from now on use the new values — "
            "nobody who already bought is re-priced."
        ),
    )
    return redirect("billing_staff:plans")


# ---------------------------------------------------------------------------
# Subscriptions and billing
# ---------------------------------------------------------------------------
# The fourth screen. The question it answers that no other screen does:
# "what does each individual subscription look like, and what has the
# provider actually told us about money".
#
# Customers is per *account* and is the comparative view with the actions
# on it. Plan and prices is per *figure* and counts states in aggregate
# ("2 awaiting first payment"), which is useless for finding *which* two.
# This one is per subscription row, and per gateway event.
#
# The hard line it holds, stated on screen as well as here: **contracted
# is not received.** The recurring total below is the sum of what active
# subscriptions are priced at — money we have agreed to bill. What has
# actually been collected is only knowable from the provider, and with no
# Asaas credentials on this installation the honest answer is that
# nothing has. Presenting the first number as the second would be the one
# mistake on this screen worse than showing no number at all.

#: Statuses the filter offers, in the order an operator wants them: the
#: ones needing a decision first. ``""`` means no filter.
SUBSCRIPTION_FILTERS = (
    "",
    WorkspaceSubscription.Status.PAST_DUE,
    WorkspaceSubscription.Status.PENDING,
    WorkspaceSubscription.Status.ACTIVE,
    WorkspaceSubscription.Status.COURTESY,
    WorkspaceSubscription.Status.CANCELED,
)

#: How many gateway events the financial history shows. Same reasoning as
#: the impersonation log's cap: a recency question, and the cap is what
#: keeps the page off a full-table scan once the provider is live and
#: sending an event per payment per month per workspace.
BILLING_EVENT_LIMIT = 100


def _event_amount_cents(payload):
    """The amount an Asaas event reports, in cents, or None.

    Extracted ONLY from ``payment.value``, which is the charge's gross
    amount and the one money field Asaas documents on every payment
    event. Deliberately not ``netValue`` (gross minus their fee — a
    different question than "what was this customer billed") and
    deliberately not inferred from the local subscription's price, which
    would be us reporting our own list price back as if the provider had
    confirmed it.

    Returns None rather than 0 whenever the field is absent, non-numeric,
    or the event has no payment object at all — a subscription event, say.
    None renders as "—" on screen; 0 would read as a free charge.

    Floats are accepted here because the provider sends them (Asaas emits
    ``"value": 50.0``) and converted to cents with rounding, which is
    exact for the two-decimal amounts it actually sends. This is the only
    place in the app that converts a float into cents, and it is doing so
    because the number arrives from outside in that shape.
    """
    payment = payload.get("payment") if isinstance(payload, dict) else None
    if not isinstance(payment, dict):
        return None
    raw = payment.get("value")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


@_staff_required
@require_GET
def staff_subscriptions(request):
    """Every subscription, one per row, plus what the provider has said.

    Three things on one screen because they are three views of the same
    money and splitting them would mean the operator cross-referencing
    two tabs to answer "is this account paying":

    1. The totals, with contracted and received kept visibly apart.
    2. One row per ``WorkspaceSubscription`` — account, workspace,
       status, monthly charge including add-ons, period end, add-on
       presence. Filterable by status, because "find the past-due ones"
       is the reason to open this screen at all.
    3. The gateway's own event log, which is the only truthful source for
       what was billed and collected.

    An unrecognised ``status`` in the query string is ignored rather than
    refused: it arrives from a hand-edited URL or a stale bookmark, and
    the useful answer to that is the unfiltered list, not a 400.
    """
    status_filter = request.GET.get("status", "")
    if status_filter not in SUBSCRIPTION_FILTERS:
        status_filter = ""

    subscriptions = (
        WorkspaceSubscription.objects.select_related("workspace", "workspace__organization")
        .prefetch_related(
            Prefetch(
                "workspace__billing_addons",
                queryset=WorkspaceAddon.objects.filter(status=WorkspaceAddon.Status.ACTIVE),
                to_attr="active_addons",
            )
        )
        .order_by("workspace__organization__name", "workspace__name")
    )
    if status_filter:
        subscriptions = subscriptions.filter(status=status_filter)

    rows = []
    for sub in subscriptions:
        addons = sub.workspace.active_addons
        rows.append(
            {
                "subscription": sub,
                "workspace": sub.workspace,
                "organization": sub.workspace.organization,
                # Through the subscription's own property, so this screen
                # and the customer's billing page cannot show different
                # monthly figures for the same workspace.
                "monthly_total_cents": sub.monthly_total_cents,
                "active_addon_count": len(addons),
                "active_addon_gb": sum(
                    addon.quantity for addon in addons if addon.kind == WorkspaceAddon.Kind.EXTRA_STORAGE
                ),
            }
        )

    # Totals computed over EVERY subscription, never over the filtered
    # queryset: a total that moves when the operator filters the list is
    # not a total, and "R$ 0,00 overdue" while filtered to Active would
    # be read as nobody being overdue.
    all_subs = WorkspaceSubscription.objects.select_related("workspace")
    storage_addons = WorkspaceAddon.objects.filter(
        kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
        status=WorkspaceAddon.Status.ACTIVE,
    )
    contracted_cents = 0
    exempt_cents = 0
    past_due_cents = 0
    for sub in all_subs:
        if sub.status == WorkspaceSubscription.Status.ACTIVE:
            contracted_cents += sub.monthly_total_cents
        elif sub.status == WorkspaceSubscription.Status.COURTESY:
            # What the comp costs us, which is what the owner wants to
            # know about a courtesy — so priced at the row's own
            # price_cents rather than through ``monthly_total_cents``,
            # which correctly returns 0 for a courtesy row.
            exempt_cents += sub.price_cents + services.addons_monthly_cents(sub.workspace_id)
        elif sub.status == WorkspaceSubscription.Status.PAST_DUE:
            past_due_cents += sub.monthly_total_cents

    events = list(
        BillingEvent.objects.select_related(
            "subscription",
            "subscription__workspace",
            "subscription__workspace__organization",
            "addon",
            "addon__workspace",
            "addon__workspace__organization",
        )[:BILLING_EVENT_LIMIT]
    )
    event_rows = []
    for event in events:
        # The workspace is known only when the webhook matched the event
        # to a local row. An event it could not place is still shown —
        # that is exactly the row somebody will need to explain — with no
        # account against it rather than a guessed one.
        linked = event.subscription or event.addon
        workspace = getattr(linked, "workspace", None)
        event_rows.append(
            {
                "event": event,
                "workspace": workspace,
                "organization": getattr(workspace, "organization", None),
                "amount_cents": _event_amount_cents(event.payload),
            }
        )

    return render(
        request,
        "billing/staff_subscriptions.html",
        {
            "rows": rows,
            "status_filter": status_filter,
            "status_choices": WorkspaceSubscription.Status.choices,
            "subscription_count": len(rows),
            "total_subscription_count": all_subs.count(),
            "totals": {
                "contracted_cents": contracted_cents,
                "exempt_cents": exempt_cents,
                "past_due_cents": past_due_cents,
            },
            # Over every subscription, never the filtered queryset: a count
            # that moves when the operator narrows the list below is not a
            # summary of anything. Same reasoning as the totals above.
            "counts": {
                "active": all_subs.filter(status=WorkspaceSubscription.Status.ACTIVE).count(),
                "courtesy": all_subs.filter(status=WorkspaceSubscription.Status.COURTESY).count(),
                "pending": all_subs.filter(status=WorkspaceSubscription.Status.PENDING).count(),
                "past_due": all_subs.filter(status=WorkspaceSubscription.Status.PAST_DUE).count(),
                "canceled": all_subs.filter(status=WorkspaceSubscription.Status.CANCELED).count(),
                "active_storage_addons": storage_addons.count(),
                # ``quantity`` is GB for the extra_storage kind — see
                # WorkspaceAddon. Coalesced to 0 because SUM over no rows
                # is NULL, and a fresh installation has no rows.
                "extra_gb_sold": storage_addons.aggregate(total=Sum("quantity"))["total"] or 0,
            },
            "event_rows": event_rows,
            "event_limit": BILLING_EVENT_LIMIT,
            "event_count": BillingEvent.objects.count(),
            # Drives the empty state's explanation. With no credentials
            # there are no payment events because none can have arrived —
            # which is a configuration fact, not a sign of customers not
            # paying, and the screen has to say which.
            "gateway_configured": bool(getattr(settings, "ASAAS_API_KEY", "")),
            "admin_active": "subscriptions",
        },
    )


@_staff_required
@require_GET
def staff_goals(request):
    """Goal and profit calculator: a projection the operator plays with.

    Nothing is stored and nothing is read back: every figure is computed in
    the browser from the inputs on screen. The view only seeds those inputs
    with today's numbers — the list price per workspace and the paying
    workspaces (``active`` subscriptions; courtesies bring no revenue) — so
    the projection starts from where the business actually is.
    """
    active = WorkspaceSubscription.objects.filter(status=WorkspaceSubscription.Status.ACTIVE)
    return render(
        request,
        "billing/staff_goals.html",
        {
            "seed": {
                "price": services.workspace_price_cents() / 100,
                "clients": active.count(),
            },
            # The chart series names, handed to the script via json_script.
            "labels": {
                "clients": _("Customers"),
                "revenue": _("Monthly revenue"),
                "profit": _("Monthly profit"),
                "goal": _("Goal"),
            },
            "admin_active": "goals",
        },
    )


@_staff_required
@require_GET
def staff_impersonation_log(request):
    """Who was inside a customer's account, and when.

    The audit question ``ImpersonationSession`` was built to answer (see
    its docstring) and the only one with no screen. Read-only by
    construction: a trail an operator can edit is not a trail, and a row
    is closed by ``impersonation.stop``, never by a person.

    Capped at the most recent rows rather than paginated. The honest
    reason is volume: takeovers are rare, and the question asked here is
    "was anyone in this account recently", which a fixed window answers.
    The cap, not pagination, is also what stops this page from becoming a
    full-table scan the day it is not.
    """
    sessions = ImpersonationSession.objects.select_related("impersonator", "impersonated")[:IMPERSONATION_LOG_LIMIT]

    return render(
        request,
        "billing/staff_impersonations.html",
        {
            "sessions": sessions,
            "limit": IMPERSONATION_LOG_LIMIT,
            "open_count": ImpersonationSession.objects.filter(ended_at__isnull=True).count(),
            "admin_active": "impersonations",
        },
    )


@_staff_required
@require_POST
def staff_toggle_courtesy(request, target_workspace_id):
    """Grant or revoke a workspace's billing exemption.

    The action is explicit in the POST (``grant``/``revoke``) rather than
    inferred from current status, so a double-submitted form is
    idempotent instead of flipping the comp back off.

    The kwarg is ``target_workspace_id`` because
    ``apps.members.middleware`` rejects any request whose URL carries a
    ``workspace_id`` the user is not a member of — and an operator comping
    a customer's workspace never is. See the note in ``staff_urls``.
    """
    workspace = get_object_or_404(Workspace.objects.select_related("organization"), id=target_workspace_id)
    action = request.POST.get("action")

    if action == "grant":
        services.grant_courtesy(
            workspace,
            note=request.POST.get("note", "").strip()[:255],
            granted_by=request.user,
        )
        # Worded as "will not be charged", matching the button that was
        # pressed. "Is now a courtesy" was read as something handed to
        # the customer rather than a line removed from their invoice.
        messages.success(
            request,
            _('Workspace "%(name)s" is now exempt and will not be charged.') % {"name": workspace.name},
        )
        if services.courtesy_quota_exceeded(workspace.organization):
            messages.warning(
                request,
                _('Account "%(org)s" now has more exempt workspaces than its limit allows.')
                % {"org": workspace.organization.name},
            )
    elif action == "revoke":
        services.revoke_courtesy(workspace)
        messages.success(
            request,
            _('Workspace "%(name)s" will be charged again. It needs a subscription to stay active.')
            % {"name": workspace.name},
        )
    else:
        messages.error(request, _("Unknown action."))

    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_set_courtesy_quota(request, org_id):
    """Set how many of an organization's EXISTING workspaces go unbilled.

    Creates nothing. The field is a ceiling on exemptions among the
    workspaces the account already has, which is also why every message
    below talks about charges rather than about granting a courtesy — the
    product owner read the old wording as provisioning workspaces.
    """
    organization = get_object_or_404(Organization, id=org_id)

    try:
        quota = int(request.POST.get("courtesy_workspace_quota", "").strip())
    except (TypeError, ValueError):
        quota = -1
    if quota < 0:
        messages.error(
            request,
            _("The number of exempt workspaces must be zero or a positive whole number."),
        )
        return redirect("billing_staff:staff_console")

    profile = services.ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = quota
    profile.courtesy_note = request.POST.get("courtesy_note", "").strip()[:255]
    profile.save(update_fields=["courtesy_workspace_quota", "courtesy_note", "updated_at"])

    messages.success(
        request,
        _('Account "%(org)s" may now have up to %(quota)s workspace(s) exempt from charges.')
        % {"org": organization.name, "quota": quota},
    )
    if services.courtesy_quota_exceeded(organization):
        messages.warning(
            request,
            _("Workspaces already exempt above the new limit were kept — nobody starts being charged retroactively."),
        )
    return redirect("billing_staff:staff_console")


# ---------------------------------------------------------------------------
# The per-account actions, all POST
# ---------------------------------------------------------------------------
# Five actions, each its own endpoint rather than one view switching on a
# hidden ``action`` field: they need different permission rules (the
# deactivation one refuses an operator target, the courtesy one does not)
# and different validation, and a single view would have had to re-derive
# which of those applied from the form.
#
# Every one is POST-only. None of them is safe to reach by navigation: a
# GET reset would let an embedded image mail a customer's reset link, and
# a GET deactivation would let any page an operator visits switch a
# customer off.


@_staff_required
@require_POST
def staff_grant_courtesy(request, org_id):
    """Release an account from charges, for a stated number of days.

    The timed version of the old quota field, and the one genuinely new
    concept on this screen: an exemption with a deadline, after which the
    account is billed again by itself. Before this, a comp granted as a
    favour stayed free until somebody remembered it.

    Three inputs, all required to mean something: how many workspaces go
    unbilled, until when, and why. The reason is stored because in six
    months "why is this agency free?" is a question only the note can
    answer.

    An empty date is accepted and means open-ended — the behaviour every
    courtesy had before this field existed, kept so an operator who wants
    a permanent comp does not have to invent a date in 2099.
    """
    organization = get_object_or_404(Organization, id=org_id)

    try:
        quota = int(request.POST.get("courtesy_workspace_quota", "").strip())
    except (TypeError, ValueError):
        quota = -1
    if quota < 0:
        messages.error(request, _("The number of exempt workspaces must be zero or a positive whole number."))
        return redirect("billing_staff:staff_console")

    raw_date = request.POST.get("courtesy_expires_on", "").strip()
    expires_on = None
    if raw_date:
        expires_on = parse_date(raw_date)
        if expires_on is None:
            messages.error(request, _("Enter the end date as a valid date, or leave it empty for no end date."))
            return redirect("billing_staff:staff_console")
        # A deadline already past would be granted and swept away by the
        # next expiry run, which looks to the operator like the form
        # silently did nothing. Refused instead of "fixed".
        if expires_on < timezone.localdate():
            messages.error(request, _("The end date is in the past. Choose today or a later date."))
            return redirect("billing_staff:staff_console")

    services.grant_account_courtesy(
        organization,
        quota=quota,
        expires_on=expires_on,
        note=request.POST.get("courtesy_note", "").strip()[:255],
        granted_by=request.user,
    )

    if expires_on is None:
        messages.success(
            request,
            _('Account "%(org)s" has %(quota)s workspace(s) exempt from charges, with no end date.')
            % {"org": organization.name, "quota": quota},
        )
    else:
        messages.success(
            request,
            _(
                'Account "%(org)s" has %(quota)s workspace(s) exempt from charges until %(date)s, '
                "after which it is billed again automatically."
            )
            % {"org": organization.name, "quota": quota, "date": date_format(expires_on, "SHORT_DATE_FORMAT")},
        )
    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_revoke_courtesy(request, org_id):
    """End an account's exemption now, ahead of any deadline.

    The reverse of the button above, in the same modal, so an operator who
    granted a comp by mistake does not have to set a date in the past to
    undo it. Runs the same service the expiry job runs, which is what
    keeps "the operator ended it" and "it ran out" from drifting apart.
    """
    organization = get_object_or_404(Organization, id=org_id)
    services.revoke_account_courtesy(organization)
    messages.success(
        request,
        _('Account "%(org)s" is billed again. Each workspace needs a subscription to stay active.')
        % {"org": organization.name},
    )
    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_edit_account(request, org_id):
    """Rename the organization and correct its owner's name and e-mail.

    The three fields support asks to fix: a typo'd signup e-mail, a
    display name, and an organization still called "My Organization".

    ``email`` is unique on ``User``, so the collision is checked here and
    reported as a message. Letting the IntegrityError out would be a 500
    on a mistake an operator makes routinely — two customers, one
    mistyped address.
    """
    organization = get_object_or_404(Organization, id=org_id)

    org_name = request.POST.get("organization_name", "").strip()
    if not org_name:
        messages.error(request, _("The account needs a name."))
        return redirect("billing_staff:staff_console")

    user = None
    raw_user_id = request.POST.get("user_id", "").strip()
    if raw_user_id:
        # Scoped to this organization's members: the org id in the URL is
        # what the operator chose, so a user id pointing anywhere else is
        # either a stale page or a tampered form, and in both cases the
        # safe answer is to refuse rather than edit a stranger.
        membership = get_object_or_404(
            OrgMembership.objects.select_related("user"),
            organization=organization,
            user_id=raw_user_id,
        )
        user = membership.user

        email = request.POST.get("email", "").strip().lower()
        if not email:
            messages.error(request, _("The account owner needs an e-mail address."))
            return redirect("billing_staff:staff_console")
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, _("That is not a valid e-mail address."))
            return redirect("billing_staff:staff_console")

        if email != user.email and User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            messages.error(
                request,
                _("Another account already uses %(email)s. Nothing was changed.") % {"email": email},
            )
            return redirect("billing_staff:staff_console")

        user.email = email
        user.name = request.POST.get("name", "").strip()[:255]
        user.save(update_fields=["email", "name", "updated_at"])

    organization.name = org_name[:100]
    organization.save(update_fields=["name", "updated_at"])

    messages.success(request, _('Account "%(org)s" updated.') % {"org": organization.name})
    if user is not None:
        logger.info("Operator %s edited account %s / user %s", request.user.email, organization.pk, user.pk)
    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_send_password_reset(request, target_user_id):
    """Mail the customer allauth's own password-reset link.

    Deliberately the *same* flow as "Forgot your password?" on the login
    page — ``allauth``'s ``ResetPasswordForm``, which builds the token,
    renders the templated mail and honours the transactional-mail
    exemption the budget backend reads. An operator-side reset written
    here instead would be a second token scheme to keep secure, and the
    first one to go stale.

    The operator never sees or sets the password: this endpoint's entire
    effect is that an e-mail goes to the address on the account. So there
    is nothing to show back, which is also why the message says a mail was
    sent rather than confirming anything about the password.

    Reported as sent even when the form refuses (an inactive account has
    no reset to send), for the same reason allauth does it on the public
    page: the reply must not become a way to probe which addresses exist.
    The refusal is logged.
    """
    target = get_object_or_404(User, pk=target_user_id)

    form = ResetPasswordForm(data={"email": target.email})
    if form.is_valid():
        form.save(request)
        logger.info("Operator %s sent a password reset to %s", request.user.email, target.email)
    else:
        logger.warning(
            "Password reset for %s not sent: %s",
            target.email,
            form.errors.as_json(),
        )

    messages.success(
        request,
        _("A password reset e-mail was sent to %(email)s. Only they can set the new password.")
        % {"email": target.email},
    )
    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_delete_user(request, target_user_id):
    """Delete a customer's account for good, with the organizations only they own.

    For test accounts and customers who asked to be erased; "Disable sign-in"
    is the reversible action. Same refusals as deactivation (``_may_deactivate``:
    never the operator's own account nor another operator's), and the email
    must be typed back, so a slip of the mouse cannot erase a customer.
    ``apps.accounts.deletion.delete_account`` cancels the organizations' Asaas
    subscriptions before deleting them; a gateway error keeps the account and
    is shown.
    """
    from apps.accounts.deletion import delete_account

    target = get_object_or_404(User, pk=target_user_id)
    operator = impersonation.real_user(request)
    if not _may_deactivate(operator, target):
        raise PermissionDenied("You may not delete this account.")

    if request.POST.get("confirm_email", "").strip().lower() != target.email.lower():
        messages.error(request, _("Type the account's email exactly to delete it."))
        return redirect("billing_staff:staff_console")

    email = target.email
    try:
        organizations = delete_account(target)
    except AsaasError:
        logger.exception(
            "Operator %s could not delete %s: Asaas refused to cancel a subscription", operator.email, email
        )
        messages.error(
            request,
            _(
                "%(email)s was not deleted: the payment provider did not cancel the subscriptions. Try again in a moment."
            )
            % {"email": email},
        )
        return redirect("billing_staff:staff_console")

    logger.warning("Operator %s deleted %s and organizations %s", operator.email, email, organizations)
    if organizations:
        messages.success(
            request,
            _("%(email)s was deleted, with %(organizations)s.")
            % {"email": email, "organizations": ", ".join(organizations)},
        )
    else:
        messages.success(request, _("%(email)s was deleted.") % {"email": email})
    return redirect("billing_staff:staff_console")


@_staff_required
@require_POST
def staff_set_user_active(request, target_user_id):
    """Switch a customer's sign-in on or off. Deletes nothing.

    ``is_active = False`` only: the organization, its workspaces, its
    posts and its media all stay exactly where they are, and flipping the
    same switch back restores access completely. That is the whole reason
    deactivation exists as an action here rather than a deletion — support
    needs to stop an account, and almost never to destroy one.

    Refuses to touch an operator account, including the one making the
    request, via the same rule the page uses to decide whether to draw
    the button (``_may_deactivate``). Self-deactivation would lock the
    operator out of the only area they can reach, and deactivating a
    colleague is privilege escalation — aimed at the last remaining
    operator it would leave the installation with no way in.
    """
    target = get_object_or_404(User, pk=target_user_id)
    operator = impersonation.real_user(request)

    if not _may_deactivate(operator, target):
        raise PermissionDenied("You may not change this account's access.")

    action = request.POST.get("action")
    if action == "deactivate":
        target.is_active = False
        target.save(update_fields=["is_active", "updated_at"])
        logger.warning("Operator %s deactivated %s", operator.email, target.email)
        messages.success(
            request,
            _("%(email)s can no longer sign in. Nothing was deleted — reactivate to restore access.")
            % {"email": target.email},
        )
    elif action == "activate":
        target.is_active = True
        target.save(update_fields=["is_active", "updated_at"])
        logger.warning("Operator %s reactivated %s", operator.email, target.email)
        messages.success(request, _("%(email)s can sign in again.") % {"email": target.email})
    else:
        messages.error(request, _("Unknown action."))

    return redirect("billing_staff:staff_console")
