"""The system e-mails the team can rewrite in Markdown, and the variables each one offers.

Each entry is keyed by the template prefix the e-mail is built from: the one
passed to ``apps.common.emails.build_email``, or allauth's prefix for its own
mail (``account/email/...``, ``socialaccount/email/...``), which goes through
``AccountAdapter.render_mail``. A variable reads one value out of the context
that e-mail is rendered with; ``sample`` is a context with made-up values for
the preview and the test send.

The daily digest is not here: it lists the day's notifications, which a fixed
text cannot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Variable:
    label: str | Promise
    read: Callable[[dict], object]


@dataclass(frozen=True)
class SystemEmail:
    key: str
    label: str | Promise
    group: str | Promise
    variables: dict[str, Variable] = field(default_factory=dict)
    sample: Callable[[], dict] = dict


def _get(context, path: str):
    value = context
    for part in path.split("."):
        if value is None:
            return ""
        value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
    return "" if value is None else value


def path(dotted: str) -> Callable[[dict], object]:
    return lambda context: _get(context, dotted)


def date(dotted: str, fmt: str = "SHORT_DATE_FORMAT") -> Callable[[dict], object]:
    def read(context):
        value = _get(context, dotted)
        if not value:
            return ""
        if hasattr(value, "tzinfo") and getattr(value, "hour", None) is not None:
            value = timezone.localtime(value) if timezone.is_aware(value) else value
        return date_format(value, fmt)

    return read


def brl(dotted: str) -> Callable[[dict], object]:
    from apps.billing.templatetags.billing_extras import brl as format_brl

    # The number only: the texts write "R$" before it, as the default templates do.
    return lambda context: format_brl(_get(context, dotted)) if _get(context, dotted) != "" else ""


def joined(dotted: str) -> Callable[[dict], object]:
    return lambda context: ", ".join(str(item) for item in (_get(context, dotted) or []))


def first_of(*readers: Callable[[dict], object]) -> Callable[[dict], object]:
    def read(context):
        for reader in readers:
            value = reader(context)
            if value:
                return value
        return ""

    return read


def _app_url() -> str:
    return getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")


#: Offered by every e-mail.
COMMON_VARIABLES = {
    "app_name": Variable(_("Product name"), path("brand_name")),
    "app_url": Variable(_("Address of the app"), path("app_url")),
    "support_email": Variable(_("Support e-mail"), path("support_email")),
}

_NAME = Variable(_("First name of the person"), first_of(path("greeting_name"), path("user.greeting_name")))
_ALLAUTH_NAME = Variable(_("First name of the person"), path("user.greeting_name"))
_CHANGE_ORIGIN = {
    "change_date": Variable(_("When the change happened"), date("timestamp", "DATETIME_FORMAT")),
    "ip_address": Variable(_("IP address of the change"), path("ip")),
    "browser": Variable(_("Browser of the change"), path("user_agent")),
    "reset_password_url": Variable(
        _("Link to reset the password"), lambda c: f"{_app_url()}{reverse('account_reset_password')}"
    ),
}

_USER = SimpleNamespace(greeting_name="Ana", email="ana@example.com", display_name="Ana Souza")
_LINK = "https://app.connektflow.com.br/exemplo/"


def _sample(**extra):
    return lambda: {"user": _USER, "greeting_name": "Ana", **extra}


_AFFILIATE_TERMS = {
    "affiliate_link": Variable(_("Affiliate link"), path("link")),
    "commission_percent": Variable(_("Commission (%)"), path("affiliate.commission_percent")),
    "commission_months": Variable(_("Months with commission"), path("affiliate.commission_months")),
    "min_payout": Variable(_("Minimum withdrawal (number, without R$)"), brl("affiliate.min_payout_cents")),
    "hold_days": Variable(_("Days before a commission enters the balance"), path("hold_days")),
}
_AFFILIATE_SAMPLE = {
    "affiliate": SimpleNamespace(
        commission_mode="recurring", commission_percent=20, commission_months=12, min_payout_cents=5000, is_active=True
    ),
    "link": "https://app.connektflow.com.br/af/ana/",
    "hold_days": 15,
}

SYSTEM_EMAILS: tuple[SystemEmail, ...] = (
    # Account and sign-in (allauth)
    SystemEmail(
        "account/email/welcome",
        _("Welcome"),
        _("Account"),
        {"name": _ALLAUTH_NAME, "email": Variable(_("E-mail of the person"), path("user.email"))},
        _sample(),
    ),
    SystemEmail(
        "account/email/email_confirmation_signup",
        _("Confirm the e-mail at sign-up"),
        _("Account"),
        {
            "name": _ALLAUTH_NAME,
            "code": Variable(_("Confirmation code"), path("code")),
            "confirm_url": Variable(_("Confirmation link"), path("activate_url")),
        },
        _sample(code="482913", activate_url=_LINK),
    ),
    SystemEmail(
        "account/email/email_confirmation",
        _("Confirm a new e-mail"),
        _("Account"),
        {
            "name": _ALLAUTH_NAME,
            "code": Variable(_("Confirmation code"), path("code")),
            "confirm_url": Variable(_("Confirmation link"), path("activate_url")),
        },
        _sample(code="482913", activate_url=_LINK),
    ),
    SystemEmail(
        "account/email/login_code",
        _("Sign-in code"),
        _("Account"),
        {"code": Variable(_("Sign-in code"), path("code"))},
        _sample(code="482913"),
    ),
    SystemEmail(
        "account/email/password_reset_key",
        _("Reset password link"),
        _("Account"),
        {"name": _ALLAUTH_NAME, "reset_url": Variable(_("Link to choose a new password"), path("password_reset_url"))},
        _sample(password_reset_url=_LINK),
    ),
    SystemEmail(
        "account/email/password_reset_code",
        _("Reset password code"),
        _("Account"),
        {"code": Variable(_("Reset code"), path("code"))},
        _sample(code="482913"),
    ),
    SystemEmail(
        "account/email/unknown_account",
        _("No account with this e-mail"),
        _("Account"),
        {
            "email": Variable(_("E-mail typed"), path("email")),
            "signup_url": Variable(_("Sign-up link"), path("signup_url")),
        },
        _sample(email="ana@example.com", signup_url=_LINK),
    ),
    SystemEmail(
        "account/email/account_already_exists",
        _("Account already exists"),
        _("Account"),
        {
            "email": Variable(_("E-mail typed"), path("email")),
            "reset_url": Variable(_("Link to reset the password"), path("password_reset_url")),
        },
        _sample(email="ana@example.com", password_reset_url=_LINK),
    ),
    SystemEmail(
        "account/email/password_changed",
        _("Password changed"),
        _("Security notices"),
        {"name": _ALLAUTH_NAME, **_CHANGE_ORIGIN},
        _sample(timestamp=timezone.now(), ip="177.20.33.10", user_agent="Chrome"),
    ),
    SystemEmail(
        "account/email/password_set",
        _("Password set"),
        _("Security notices"),
        {"name": _ALLAUTH_NAME, **_CHANGE_ORIGIN},
        _sample(timestamp=timezone.now(), ip="177.20.33.10", user_agent="Chrome"),
    ),
    SystemEmail(
        "account/email/password_reset",
        _("Password reset"),
        _("Security notices"),
        {"name": _ALLAUTH_NAME, **_CHANGE_ORIGIN},
        _sample(timestamp=timezone.now(), ip="177.20.33.10", user_agent="Chrome"),
    ),
    SystemEmail(
        "account/email/email_changed",
        _("E-mail changed"),
        _("Security notices"),
        {
            "old_email": Variable(_("Previous e-mail"), path("from_email")),
            "new_email": Variable(_("New e-mail"), path("to_email")),
            **_CHANGE_ORIGIN,
        },
        _sample(from_email="antigo@example.com", to_email="novo@example.com", timestamp=timezone.now()),
    ),
    SystemEmail(
        "account/email/email_confirm",
        _("E-mail confirmed"),
        _("Security notices"),
        dict(_CHANGE_ORIGIN),
        _sample(timestamp=timezone.now()),
    ),
    SystemEmail(
        "account/email/email_deleted",
        _("E-mail removed"),
        _("Security notices"),
        {"removed_email": Variable(_("Removed e-mail"), path("deleted_email")), **_CHANGE_ORIGIN},
        _sample(deleted_email="antigo@example.com", timestamp=timezone.now()),
    ),
    SystemEmail(
        "socialaccount/email/account_connected",
        _("Google sign-in connected"),
        _("Security notices"),
        {"provider": Variable(_("Sign-in provider"), path("provider.name")), **_CHANGE_ORIGIN},
        _sample(provider=SimpleNamespace(name="Google"), timestamp=timezone.now()),
    ),
    SystemEmail(
        "socialaccount/email/account_disconnected",
        _("Google sign-in disconnected"),
        _("Security notices"),
        {"provider": Variable(_("Sign-in provider"), path("provider.name")), **_CHANGE_ORIGIN},
        _sample(provider=SimpleNamespace(name="Google"), timestamp=timezone.now()),
    ),
    # Team and organization
    SystemEmail(
        "members/email/invite",
        _("Team invitation"),
        _("Team and organization"),
        {
            "name": Variable(_("Name of the person invited"), path("invitation.name")),
            "email": Variable(_("E-mail invited"), path("invitation.email")),
            "inviter_name": Variable(_("Who invited"), path("inviter_name")),
            "organization_name": Variable(_("Organization"), path("organization_name")),
            "brand": Variable(_("Brand (only in brand invitations)"), path("brand.name")),
            "accept_url": Variable(_("Link to accept"), path("accept_url")),
        },
        lambda: {
            "invitation": SimpleNamespace(name="Ana", email="ana@example.com"),
            "inviter_name": "Robison Borges",
            "organization_name": "CAMILA AP",
            "brand": None,
            "accept_url": _LINK,
        },
    ),
    SystemEmail(
        "organizations/email/deletion_scheduled",
        _("Organization deletion scheduled"),
        _("Team and organization"),
        {
            "name": _NAME,
            "organization_name": Variable(_("Organization"), path("organization.name")),
            "deletion_date": Variable(_("Date of the deletion"), date("deletion_at")),
            "brands": Variable(_("Brands deleted with it"), joined("brand_names")),
            "youtube_channels": Variable(_("YouTube channels disconnected"), joined("removed_youtube")),
            "networks": Variable(_("Networks still connected"), joined("remaining_networks")),
            "restore_url": Variable(_("Link to cancel the deletion"), path("restore_url")),
        },
        _sample(
            organization=SimpleNamespace(name="CAMILA AP"),
            deletion_at=timezone.now() + timedelta(days=14),
            brand_names=["Loja Centro", "Ateliê"],
            removed_youtube=[],
            remaining_networks=["Instagram"],
            restore_url=_LINK,
        ),
    ),
    SystemEmail(
        "onboarding/email/connection_link",
        _("Link to connect accounts"),
        _("Team and organization"),
        {
            "organization_name": Variable(_("Organization"), path("org_name")),
            "brand": Variable(_("Brand"), path("workspace_name")),
            "link_url": Variable(_("Connection link"), path("link_url")),
            "expires": Variable(_("Link expiry"), date("expires_at")),
        },
        lambda: {
            "org_name": "CAMILA AP",
            "workspace_name": "Loja Centro",
            "link_url": _LINK,
            "expires_at": timezone.now() + timedelta(days=7),
        },
    ),
    SystemEmail(
        "client_portal/email/magic_link",
        _("Client portal link"),
        _("Team and organization"),
        {
            "client_name": Variable(_("Client name"), path("client_user.display_name")),
            "sender_name": Variable(_("Who shared"), path("created_by.display_name")),
            "brand": Variable(_("Brand"), path("workspace.name")),
            "portal_url": Variable(_("Portal link"), path("magic_url")),
        },
        lambda: {
            "client_user": SimpleNamespace(display_name="Ana Cliente"),
            "created_by": SimpleNamespace(display_name="Robison Borges"),
            "workspace": SimpleNamespace(name="Loja Centro"),
            "magic_url": _LINK,
        },
    ),
    SystemEmail(
        "notifications/email/notification",
        _("Notification"),
        _("Team and organization"),
        {
            "title": Variable(_("Notification title"), path("notification.title")),
            "body": Variable(_("Notification text"), path("notification.body")),
            "action_url": Variable(_("Notification link"), path("notification.data.action_url")),
            "email": Variable(_("E-mail of the person"), path("user.email")),
        },
        _sample(
            notification=SimpleNamespace(
                title="Post publicado", body="O post foi publicado no Instagram.", data={"action_url": _LINK}
            )
        ),
    ),
    # Billing
    SystemEmail(
        "billing/email/payment_confirmed",
        _("Payment confirmed"),
        _("Billing"),
        {
            "name": _NAME,
            "amount": Variable(_("Amount charged"), brl("plan.charge_cents")),
            "brands": Variable(_("Number of brands"), path("plan.brands")),
            "paid_for": Variable(_("What was paid for"), path("paid_for")),
            "period_end": Variable(_("Paid until"), date("plan.current_period_end")),
            "billing_url": Variable(_("Billing page"), path("billing_url")),
        },
        _sample(
            plan=SimpleNamespace(charge_cents=29700, brands=3, current_period_end=timezone.now() + timedelta(days=30)),
            paid_for="CAMILA AP",
            billing_url=_LINK,
        ),
    ),
    SystemEmail(
        "billing/email/archive_removal_warning",
        _("Data removal warning"),
        _("Billing"),
        {
            "name": _NAME,
            "organization_name": Variable(_("Organization"), path("organization.name")),
            "removal_date": Variable(_("Date of the removal"), date("removal_at")),
            "billing_url": Variable(_("Billing page"), path("billing_url")),
        },
        _sample(
            organization=SimpleNamespace(name="CAMILA AP"),
            removal_at=timezone.now() + timedelta(days=7),
            billing_url=_LINK,
        ),
    ),
    # Affiliates
    SystemEmail(
        "affiliates/email/affiliate_added",
        _("New affiliate"),
        _("Affiliates"),
        {"name": _NAME, "panel_url": Variable(_("Affiliate panel"), path("panel_url")), **_AFFILIATE_TERMS},
        _sample(panel_url=_LINK, **_AFFILIATE_SAMPLE),
    ),
    SystemEmail(
        "affiliates/email/terms_changed",
        _("Affiliate terms changed"),
        _("Affiliates"),
        {"name": _NAME, "panel_url": Variable(_("Affiliate panel"), path("panel_url")), **_AFFILIATE_TERMS},
        _sample(panel_url=_LINK, **_AFFILIATE_SAMPLE),
    ),
    SystemEmail(
        "affiliates/email/payout_requested",
        _("Withdrawal requested"),
        _("Affiliates"),
        {
            "name": _NAME,
            "amount": Variable(_("Amount"), brl("payout.amount_cents")),
            "deadline": Variable(_("Payment deadline"), date("deadline")),
            "payout_days": Variable(_("Days to pay"), path("payout_days")),
            "pix_key": Variable(_("Pix key"), path("payout.pix_key")),
            "withdrawals_url": Variable(_("Withdrawals page"), path("withdrawals_url")),
        },
        _sample(
            payout=SimpleNamespace(amount_cents=15000, pix_key="ana@example.com"),
            deadline=timezone.now() + timedelta(days=5),
            payout_days=5,
            withdrawals_url=_LINK,
        ),
    ),
    SystemEmail(
        "affiliates/email/payout_paid",
        _("Withdrawal paid"),
        _("Affiliates"),
        {
            "name": _NAME,
            "amount": Variable(_("Amount"), brl("payout.amount_cents")),
            "pix_key": Variable(_("Pix key"), path("payout.pix_key")),
            "paid_date": Variable(_("Date paid"), date("payout.resolved_at")),
            "withdrawals_url": Variable(_("Withdrawals page"), path("withdrawals_url")),
        },
        _sample(
            payout=SimpleNamespace(amount_cents=15000, pix_key="ana@example.com", resolved_at=timezone.now(), note=""),
            withdrawals_url=_LINK,
        ),
    ),
    SystemEmail(
        "affiliates/email/payout_requested_team",
        _("Withdrawal to pay (team)"),
        _("Affiliates"),
        {
            "affiliate_name": Variable(_("Affiliate"), path("payout.affiliate.name")),
            "amount": Variable(_("Amount"), brl("payout.amount_cents")),
            "pix_key": Variable(_("Pix key"), path("payout.pix_key")),
            "deadline": Variable(_("Payment deadline"), date("deadline")),
            "review_url": Variable(_("Link to the withdrawal"), path("review_url")),
        },
        lambda: {
            "payout": SimpleNamespace(
                amount_cents=15000, pix_key="ana@example.com", affiliate=SimpleNamespace(name="Ana Souza")
            ),
            "deadline": timezone.now() + timedelta(days=5),
            "review_url": _LINK,
        },
    ),
)

BY_KEY = {email.key: email for email in SYSTEM_EMAILS}


def get(key: str) -> SystemEmail | None:
    return BY_KEY.get(key)


def all_variables(entry: SystemEmail) -> dict[str, Variable]:
    return {**COMMON_VARIABLES, **entry.variables}


def resolve(entry: SystemEmail, context: dict) -> dict[str, str]:
    """The value of each of the e-mail's variables in ``context``, as text."""
    values = {}
    for name, variable in all_variables(entry).items():
        try:
            value = variable.read(context)
        except Exception:  # noqa: BLE001 — a variable that cannot be read is left empty, never breaks a send
            value = ""
        values[name] = "" if value is None else str(value)
    return values
