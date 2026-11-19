"""Billing domain logic.

Everything here is pure local computation — quota arithmetic, price
lookups, courtesy grants. No gateway calls: the Asaas client arrives in
its own module and will call into these functions, not the other way
round, so the money rules stay testable without a network.

Units, held without exception: money in integer cents, disk in integer
bytes.
"""

from __future__ import annotations

import datetime
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _

from apps.billing.models import (
    BillingEvent,
    Coupon,
    CouponRedemption,
    OrganizationBillingProfile,
    OrganizationPlan,
    ProductPrice,
    ProductPriceChange,
    WorkspaceAddon,
    WorkspaceSubscription,
)
from apps.contacts.forms import is_valid_cnpj, is_valid_cpf

# ---------------------------------------------------------------------------
# Price list
# ---------------------------------------------------------------------------
# Three figures, each readable from one function, and those three
# functions are the only place in the codebase that asks what something
# costs. That is what lets the price list move from settings into the
# database without touching a single call site.
#
# Resolution order, per figure: the ``ProductPrice`` row if one exists,
# otherwise the deployment setting. Absence — not a sentinel value — is
# what means "nobody has set this", so a fresh installation charges the
# configured default and the default is preserved rather than copied into
# the database by a migration.

#: Which setting backs each key when no row has been written. The mapping
#: lives here rather than on the model so the model stays free of
#: deployment concerns and this module keeps the whole fallback rule in
#: one readable place.
_PRICE_FALLBACK_SETTINGS = {
    ProductPrice.Key.WORKSPACE_PRICE_CENTS: "BILLING_WORKSPACE_PRICE_CENTS",
    ProductPrice.Key.BASE_STORAGE_BYTES: "BILLING_WORKSPACE_BASE_STORAGE_BYTES",
    ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS: "BILLING_STORAGE_PRICE_PER_GB_CENTS",
}

#: Hard ceilings, per key, on what an operator may save. Not opinions
#: about the business: they are the "absurd input" guard. R$ 100.000,00 a
#: month per workspace and R$ 10.000,00 a GB are both far past anything
#: this product would charge, and 100 TB of included disk is past what it
#: could serve — so a value above them is a typo (a price pasted with its
#: cents twice, a byte figure typed into a GB field) rather than a
#: decision, and refusing is kinder than writing it.
PRICE_MAX_VALUES = {
    ProductPrice.Key.WORKSPACE_PRICE_CENTS: 10_000_000,
    ProductPrice.Key.BASE_STORAGE_BYTES: 100 * 1_000_000_000_000,
    ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS: 1_000_000,
}

#: Cached because the three readers run in a loop on the customers screen
#: (once per workspace per account) and on every storage check. The TTL is
#: a backstop only: ``set_price`` invalidates on write, so a stale figure
#: can outlive the truth by at most this long and only if an invalidation
#: is missed entirely.
_PRICE_CACHE_SECONDS = 300

#: Sentinel stored in the cache for "there is no row for this key", so a
#: fresh installation (the normal state) does not re-query on every read
#: just because the honest answer is absence. ``None`` cannot be used —
#: ``cache.get`` returns it for a miss.
_PRICE_ABSENT = -1


def _price_cache_key(key):
    return f"billing:product_price:{key}"


def _configured_price(key):
    """The stored value for ``key``, or None when nobody has set one.

    None is the whole point: it is what sends the caller to the
    deployment setting, and it is distinguishable from a stored 0 — which
    is a legitimate price (a figure the owner has deliberately zeroed)
    and must not silently fall back to 5000.
    """
    cache_key = _price_cache_key(key)
    cached = cache.get(cache_key)
    if cached is not None:
        return None if cached == _PRICE_ABSENT else int(cached)

    row = ProductPrice.objects.filter(key=key).only("value").first()
    value = None if row is None else int(row.value)
    cache.set(cache_key, _PRICE_ABSENT if value is None else value, _PRICE_CACHE_SECONDS)
    return value


def _resolved_price(key):
    """``key``'s effective value: the stored row, else the setting."""
    value = _configured_price(key)
    if value is not None:
        return value
    return int(getattr(settings, _PRICE_FALLBACK_SETTINGS[key]))


def invalidate_price_cache(key=None):
    """Drop the cached price list, or one key of it.

    Deleting rather than rewriting, for the same reason the storage-usage
    cache does: the next reader pays for one query, and a correct read is
    worth more than a saved one on a figure that changes a few times a
    year.
    """
    keys = [key] if key is not None else list(ProductPrice.Key.values)
    cache.delete_many([_price_cache_key(k) for k in keys])


def workspace_price_cents():
    """List price of one workspace per month, in cents."""
    return _resolved_price(ProductPrice.Key.WORKSPACE_PRICE_CENTS)


def base_storage_bytes():
    """Disk included with every workspace, in bytes."""
    return _resolved_price(ProductPrice.Key.BASE_STORAGE_BYTES)


def storage_price_per_gb_cents():
    """Current list price of one extra GB per month, in cents.

    Read only when contracting a new add-on. The resulting row keeps its
    own ``unit_price_cents``, so changing this value never re-prices an
    add-on already sold.
    """
    return _resolved_price(ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS)


def price_list():
    """Every figure the operator can edit, with where it came from.

    One query for the lot, so the screen does not do three. ``source`` is
    reported because "R$ 50,00 because nobody has decided yet" and
    "R$ 50,00 because the owner chose it" are different facts, and the
    screen should not present the first as the second.
    """
    rows = {row.key: row for row in ProductPrice.objects.select_related("updated_by")}
    entries = {}
    for key, setting_name in _PRICE_FALLBACK_SETTINGS.items():
        row = rows.get(key)
        entries[key] = {
            "key": key,
            "value": int(row.value) if row else int(getattr(settings, setting_name)),
            "is_configured": row is not None,
            "updated_at": row.updated_at if row else None,
            "updated_by": row.updated_by if row else None,
            "default_value": int(getattr(settings, setting_name)),
        }
    return entries


# The refusal wording, per key and per reason. Written out per key rather
# than built from a generic template because each figure has its own unit
# and the unit is most of the content of the message. Lazy so the module
# imports before the translations are active; resolved when the message is
# handed to ``messages.error``.
#
# Aliased ``_`` and not something more descriptive: ``xgettext`` only
# recognises the conventional names, so an alias like ``_lazy`` extracts
# nothing and every message below would render in English forever.
#
# Every money message talks in reais, never in cents: cents are how this
# app STORES money, and the operator is not writing the storage layer. The
# screen asks for R$ 50,00 and so does the refusal.
_PRICE_MESSAGES = {
    ProductPrice.Key.WORKSPACE_PRICE_CENTS: {
        "required": _("Enter the price per workspace, in reais."),
        "not_a_number": _("The price per workspace is not a valid amount. Enter it as 50,00."),
        "negative": _("The price per workspace cannot be negative. Use 0 to charge nothing."),
        "too_precise": _("The price per workspace cannot have fractions of a cent. Use at most two decimals."),
        "too_large": _("That price per workspace is too high to be intentional. Check the amount."),
    },
    ProductPrice.Key.BASE_STORAGE_BYTES: {
        "required": _("Enter how much disk is included, in GB."),
        "not_a_number": _("The included disk must be a whole number of GB."),
        "negative": _("The included disk cannot be negative. Use 0 to include none."),
        "too_precise": _("The included disk must be a whole number of GB."),
        "too_large": _("That amount of included disk is too high to be intentional. Check the value, in GB."),
    },
    ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS: {
        "required": _("Enter the price per extra GB, in reais."),
        "not_a_number": _("The price per extra GB is not a valid amount. Enter it as 2,00."),
        "negative": _("The price per extra GB cannot be negative. Use 0 to give extra disk away."),
        "too_precise": _("The price per extra GB cannot have fractions of a cent. Use at most two decimals."),
        "too_large": _("That price per extra GB is too high to be intentional. Check the amount."),
    },
}


class PriceValidationError(ValueError):
    """An operator-supplied price that must not be written.

    A dedicated exception rather than returning None so a caller cannot
    accidentally treat "refused" as "unchanged", and so the message that
    reaches the operator is written once, next to the rule it breaks.
    """


#: The unit each figure is TYPED in, as opposed to the unit it is stored
#: in. Both differ from storage, and for the same reason: nobody types ten
#: zeroes for ten gigabytes, and nobody thinks about a price in cents.
#:
#: Money is stored in cents and disk in bytes — the convention this whole
#: app holds and which does not change here. The conversion happens in
#: ``parse_price_value``, in the same function that produces the error
#: message, so the unit the operator was asked for and the unit the
#: refusal names can never disagree.
_INPUT_UNIT_CURRENCY = "currency"
_INPUT_UNIT_GB = "gb"

_PRICE_INPUT_UNITS = {
    ProductPrice.Key.WORKSPACE_PRICE_CENTS: _INPUT_UNIT_CURRENCY,
    ProductPrice.Key.BASE_STORAGE_BYTES: _INPUT_UNIT_GB,
    ProductPrice.Key.STORAGE_PRICE_PER_GB_CENTS: _INPUT_UNIT_CURRENCY,
}

#: Everything a currency field may contain besides the digits and the
#: decimal mark. Stripped rather than refused because a person typing into
#: a masked field produces all of it: the mask itself writes "R$" and the
#: thousands dots, and a paste from a spreadsheet brings a non-breaking
#: space along.
_CURRENCY_NOISE = str.maketrans({"R": None, "$": None, " ": None, " ": None, " ": None})

#: Two decimals, rounded half-up, which is the rounding a person expects
#: of money and not the banker's rounding ``Decimal`` defaults to.
_CENTS_EXPONENT = Decimal("0.01")

#: Magnitude past which a parsed amount is refused before any arithmetic
#: touches it. Exists for a mechanical reason, not a business one:
#: ``Decimal("1e400").quantize(Decimal("0.01"))`` raises InvalidOperation
#: rather than returning a number, so an exponent typed into the field
#: would be a 500 instead of a message. Comfortably above every per-key
#: ceiling in ``PRICE_MAX_VALUES``, which is what actually decides
#: "too high to be intentional".
_CURRENCY_PARSE_CEILING = Decimal(10) ** 15


def _parse_currency_to_cents(text, messages_for_key):
    """``text`` as an integer number of cents, pt-BR conventions.

    Accepts everything a person naturally types into a currency field:
    ``50``, ``50,00``, ``R$ 50,00``, ``1.234,56``, and — tolerated
    because a keyboard's numeric pad produces it and a paste from an
    English spreadsheet does too — ``50.00``.

    ``Decimal`` throughout, never ``float``: ``float("0.07") * 100`` is
    7.000000000000001, and ``int()`` of that is 7 only by luck of which
    direction the error went. On money the luck is not acceptable.

    The comma/dot ambiguity is resolved by position, not by guessing: a
    comma is always the decimal mark (pt-BR), so any dot in a string
    containing a comma is a thousands separator. With no comma, a single
    dot followed by one or two digits is read as a decimal mark — which
    is what makes ``50.00`` work — and anything else is thousands.
    """
    cleaned = text.translate(_CURRENCY_NOISE)
    if not cleaned:
        raise PriceValidationError(messages_for_key["required"])

    if "," in cleaned:
        # pt-BR: comma decides, so every dot is a thousands separator.
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") == 1:
        whole, _, fraction = cleaned.partition(".")
        # "50.00" is R$ 50,00; "1.234" is R$ 1.234,00. Decided by the
        # length of the group after the dot, which is the only signal
        # there is — and the one a human reading it would use.
        if len(fraction) == 3 and whole:
            cleaned = whole + fraction
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise PriceValidationError(messages_for_key["not_a_number"]) from None
    # Decimal accepts "NaN" and "Infinity" without complaint, and both
    # would sail past the comparisons below.
    if not amount.is_finite():
        raise PriceValidationError(messages_for_key["not_a_number"])
    if amount < 0:
        raise PriceValidationError(messages_for_key["negative"])
    # Bounded BEFORE quantizing, and this ordering is load-bearing:
    # ``Decimal("1e400").quantize(...)`` raises InvalidOperation rather
    # than returning a number, so a precision check placed first turns a
    # typo into a 500. The ceiling is re-checked in stored units by the
    # caller; this one only has to keep the arithmetic below in range.
    if amount > _CURRENCY_PARSE_CEILING:
        raise PriceValidationError(messages_for_key["too_large"])
    # Refused rather than rounded: "0,005" is somebody who meant
    # something, and silently charging them 0 or 1 cent is guessing at
    # which. Compared against the quantized value, which is what makes a
    # third decimal visible at all.
    quantized = amount.quantize(_CENTS_EXPONENT, rounding=ROUND_HALF_UP)
    if amount != quantized:
        raise PriceValidationError(messages_for_key["too_precise"])

    return int(quantized * 100)


def _parse_gb_to_bytes(text, messages_for_key):
    """``text`` as an integer number of bytes from a whole-GB figure.

    Whole GB only: the plan is quoted in gigabytes and a fractional one
    has never been sold, so "10,5" is far more likely a typo than an
    intent to include 10.5 GB.
    """
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise PriceValidationError(messages_for_key["not_a_number"]) from None
    if value < 0:
        raise PriceValidationError(messages_for_key["negative"])
    return value * WorkspaceAddon.BYTES_PER_GB


def parse_price_value(key, raw):
    """A validated stored-unit integer for ``key`` from raw form input.

    Every refusal is a message the operator can act on — the point is
    that bad input never reaches the database and never becomes a 500.
    Empty, non-numeric, negative, over-precise and absurd are all refused
    here, so the view has no validation of its own to get wrong.

    The bound is checked after conversion, against ``PRICE_MAX_VALUES``,
    which is in stored units — so one ceiling covers the figure however
    it was typed.
    """
    messages_for_key = _PRICE_MESSAGES[key]
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise PriceValidationError(messages_for_key["required"])

    if _PRICE_INPUT_UNITS[key] == _INPUT_UNIT_CURRENCY:
        value = _parse_currency_to_cents(text, messages_for_key)
    else:
        value = _parse_gb_to_bytes(text, messages_for_key)

    if value > PRICE_MAX_VALUES[key]:
        raise PriceValidationError(messages_for_key["too_large"])
    return value


def price_input_value(key, stored_value):
    """``stored_value`` back in the unit the form asks for.

    The inverse of :func:`parse_price_value`, so a field redisplays what
    the operator typed rather than the cents or bytes it was stored as.

    Money comes back as a ``Decimal`` with two places, which the template
    then renders through the locale — a string built here would hardcode
    one locale's decimal mark into the value the form posts back.
    """
    stored_value = int(stored_value)
    if _PRICE_INPUT_UNITS[key] == _INPUT_UNIT_CURRENCY:
        return (Decimal(stored_value) / 100).quantize(_CENTS_EXPONENT)
    # Exact: the only way to write this key is through the whole-GB field.
    return stored_value // WorkspaceAddon.BYTES_PER_GB


def set_price(key, value, *, changed_by=None):
    """Write one list price and record who changed it from what.

    The row and its history entry are written in one transaction: a price
    that moved with no trail, or a trail for a price that did not move,
    are both worse than the edit failing. Returns ``(price, change)``,
    with ``change`` None when the value was already what was asked for —
    a resubmitted form is then idempotent instead of filling the history
    with no-ops.

    Nothing already sold is touched, and nothing here could touch it:
    every ``WorkspaceSubscription`` and ``WorkspaceAddon`` carries its own
    price column, written once at contracting time. That invariant is the
    reason this function exists at all rather than an UPDATE across the
    sold rows.
    """
    if key not in ProductPrice.Key.values:
        raise PriceValidationError("Unknown price key.")
    value = int(value)

    with transaction.atomic():
        row = ProductPrice.objects.select_for_update().filter(key=key).first()
        previous = None if row is None else int(row.value)
        if previous == value:
            return row, None

        if row is None:
            row = ProductPrice(key=key)
        row.value = value
        row.updated_by = changed_by
        row.save()

        change = ProductPriceChange.objects.create(
            key=key,
            previous_value=previous,
            new_value=value,
            changed_by=changed_by,
            changed_by_email=getattr(changed_by, "email", "") or "",
        )

    # After the commit, so a reader that races the write cannot repopulate
    # the cache from the pre-commit snapshot and then be invalidated.
    invalidate_price_cache(key)
    return row, change


def recent_price_changes(limit=20):
    """The latest edits to the price list, newest first.

    A window rather than pagination: the question the screen asks is
    "what moved recently, and who moved it", and price edits are rare
    enough that a fixed window answers it for years.
    """
    return list(ProductPriceChange.objects.select_related("changed_by")[:limit])


# ---------------------------------------------------------------------------
# Billing rules — discounts, trial and the subscription lock
# ---------------------------------------------------------------------------
#
# Edited on the plan screen like the prices and stored the same way: one
# ProductPrice row per figure, the change recorded in ProductPriceChange, no
# row meaning "nobody set it" and falling back to the BILLING_* setting. Like
# a price, a change applies to what is contracted from then on.

VOLUME_TIER_KEYS = (
    (ProductPrice.Key.VOLUME_1_FROM, ProductPrice.Key.VOLUME_1_PERCENT),
    (ProductPrice.Key.VOLUME_2_FROM, ProductPrice.Key.VOLUME_2_PERCENT),
    (ProductPrice.Key.VOLUME_3_FROM, ProductPrice.Key.VOLUME_3_PERCENT),
)

#: Upper bounds of the whole-number rules, the same guard the deployment
#: settings apply (config/settings/base.py) plus a year for the trial.
RULE_MAX_PERCENT = 90
RULE_MAX_TRIAL_DAYS = 365
RULE_MAX_VOLUME_FROM = 1000


def _rule_default(key):
    """The deployment setting behind ``key``, in the stored unit."""
    if key == ProductPrice.Key.ANNUAL_DISCOUNT_PERCENT:
        return int(settings.BILLING_ANNUAL_DISCOUNT_PERCENT)
    if key == ProductPrice.Key.TRIAL_DAYS:
        return int(settings.BILLING_TRIAL_DAYS)
    if key == ProductPrice.Key.REQUIRE_SUBSCRIPTION:
        return int(bool(settings.BILLING_REQUIRE_SUBSCRIPTION))
    tiers = list(settings.BILLING_VOLUME_DISCOUNTS)
    for index, (from_key, percent_key) in enumerate(VOLUME_TIER_KEYS):
        if key in (from_key, percent_key):
            if index >= len(tiers):
                return 0
            return int(tiers[index][0] if key == from_key else tiers[index][1])
    raise KeyError(key)


def _resolved_rule(key):
    value = _configured_price(key)
    return _rule_default(key) if value is None else value


def billing_rules():
    """The rules as the plan screen's form shows them, with whether each was set there."""
    tiers = volume_discounts()
    volume = [
        {
            "index": index + 1,
            "from": tiers[index][0] if index < len(tiers) else "",
            "percent": tiers[index][1] if index < len(tiers) else "",
        }
        for index in range(len(VOLUME_TIER_KEYS))
    ]
    keys = [
        ProductPrice.Key.ANNUAL_DISCOUNT_PERCENT,
        ProductPrice.Key.TRIAL_DAYS,
        ProductPrice.Key.REQUIRE_SUBSCRIPTION,
    ]
    keys += [key for pair in VOLUME_TIER_KEYS for key in pair]
    return {
        "annual_discount_percent": annual_discount_percent(),
        "trial_days": trial_days(),
        "subscription_required": subscription_required(),
        "volume": volume,
        "is_configured": ProductPrice.objects.filter(key__in=keys).exists(),
    }


def _whole_number(raw, *, maximum, message):
    text = "" if raw is None else str(raw).strip()
    if not text:
        return 0
    if not text.isdigit() or int(text) > maximum:
        raise PriceValidationError(message)
    return int(text)


def parse_billing_rules(data):
    """``{key: value}`` for every rule from the plan screen's form, or PriceValidationError.

    Empty numbers mean 0. A volume tier is either empty (both 0) or a
    paid-workspace position from 2 with a percentage from 1 to 90, and two
    tiers cannot start at the same position.
    """
    values = {
        ProductPrice.Key.ANNUAL_DISCOUNT_PERCENT: _whole_number(
            data.get("annual_discount_percent"),
            maximum=RULE_MAX_PERCENT,
            message=_("The yearly discount must be a whole percentage from 0 to 90."),
        ),
        ProductPrice.Key.TRIAL_DAYS: _whole_number(
            data.get("trial_days"),
            maximum=RULE_MAX_TRIAL_DAYS,
            message=_("The free trial must be a whole number of days from 0 to 365."),
        ),
        ProductPrice.Key.REQUIRE_SUBSCRIPTION: 1 if data.get("subscription_required") == "on" else 0,
    }
    starts = set()
    for index, (from_key, percent_key) in enumerate(VOLUME_TIER_KEYS, start=1):
        minimum = _whole_number(
            data.get(f"volume_{index}_from"),
            maximum=RULE_MAX_VOLUME_FROM,
            message=_("A volume discount starts at a whole number of paid brands, from 2."),
        )
        percent = _whole_number(
            data.get(f"volume_{index}_percent"),
            maximum=RULE_MAX_PERCENT,
            message=_("A volume discount must be a whole percentage from 1 to 90."),
        )
        if bool(minimum) != bool(percent):
            raise PriceValidationError(_("Fill in both the brand and the percentage of a volume discount, or neither."))
        if minimum and minimum < 2:
            raise PriceValidationError(_("A volume discount starts at a whole number of paid brands, from 2."))
        if minimum and minimum in starts:
            raise PriceValidationError(_("Two volume discounts cannot start at the same brand."))
        if minimum:
            starts.add(minimum)
        values[from_key] = minimum
        values[percent_key] = percent
    return values


# ---------------------------------------------------------------------------
# Quotes — what contracting a workspace costs right now
# ---------------------------------------------------------------------------

#: Statuses of a subscription that counts toward the volume discount once
#: it has been contracted at the gateway. Courtesy and canceled do not:
#: the discount rewards workspaces being paid for.
_PAYING_STATUSES = (
    WorkspaceSubscription.Status.PENDING,
    WorkspaceSubscription.Status.ACTIVE,
    WorkspaceSubscription.Status.PAST_DUE,
)


def annual_discount_percent():
    """Discount of the yearly plan, in whole percent. 0 = not offered."""
    return _resolved_rule(ProductPrice.Key.ANNUAL_DISCOUNT_PERCENT)


def volume_discounts():
    """The volume-discount tiers as ``[(from_position, percent), ...]``, ascending.

    The operator's tiers once the rules form has been saved (it writes all
    three), else ``BILLING_VOLUME_DISCOUNTS``. A tier saved as 0 and 0 is unused.
    """
    stored = [
        (_configured_price(from_key), _configured_price(percent_key)) for from_key, percent_key in VOLUME_TIER_KEYS
    ]
    if all(minimum is None and percent is None for minimum, percent in stored):
        return list(settings.BILLING_VOLUME_DISCOUNTS)
    return sorted((minimum, percent) for minimum, percent in stored if minimum and percent)


def volume_discount_percent(position):
    """Discount of an organization's ``position``-th paid workspace."""
    percent = 0
    for minimum, tier_percent in volume_discounts():
        if position >= minimum:
            percent = tier_percent
    return percent


def paid_workspace_count(organization_id, *, exclude_workspace_id=None):
    """How many of an organization's workspaces are contracted and paying."""
    queryset = WorkspaceSubscription.objects.filter(
        workspace__organization_id=organization_id,
        status__in=_PAYING_STATUSES,
    ).exclude(gateway_subscription_id="")
    if exclude_workspace_id is not None:
        queryset = queryset.exclude(workspace_id=exclude_workspace_id)
    return queryset.count()


def quote_workspace(workspace, cycle=WorkspaceSubscription.Cycle.MONTHLY):
    """What contracting ``workspace`` on ``cycle`` costs at today's list.

    The one place the discounts are applied, so the figure shown on the
    contracting page and the figure sent to the gateway cannot differ.
    The volume discount is decided by the position this workspace would
    take among the organization's paying ones; the yearly discount is
    applied on top. ``monthly_cents`` is rounded to a whole cent first and
    the yearly charge is twelve times it, so "R$ X a month" times twelve
    is exactly what is charged.
    """
    list_cents = workspace_price_cents()
    position = paid_workspace_count(workspace.organization_id, exclude_workspace_id=workspace.pk) + 1
    volume_percent = volume_discount_percent(position)
    yearly = cycle == WorkspaceSubscription.Cycle.YEARLY
    annual_percent = annual_discount_percent() if yearly else 0

    monthly = Decimal(list_cents) * (100 - volume_percent) * (100 - annual_percent) / 10000
    monthly_cents = int(monthly.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return {
        "cycle": WorkspaceSubscription.Cycle.YEARLY if yearly else WorkspaceSubscription.Cycle.MONTHLY,
        "list_cents": list_cents,
        "position": position,
        "volume_percent": volume_percent,
        "annual_percent": annual_percent,
        "monthly_cents": monthly_cents,
        "charge_cents": monthly_cents * 12 if yearly else monthly_cents,
        # Against paying the same workspace month by month for a year.
        "yearly_saving_cents": (
            int((Decimal(list_cents) * (100 - volume_percent) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            - monthly_cents
        )
        * 12,
    }


# ---------------------------------------------------------------------------
# Organization plan — brands bought together
# ---------------------------------------------------------------------------

#: The most brands one checkout sells. Past it the operator talks to the customer.
PLAN_MAX_BRANDS = 50


def quote_plan(brands, cycle=WorkspaceSubscription.Cycle.MONTHLY, coupon=None):
    """What ``brands`` brands bought together on ``cycle`` cost at today's list.

    The checkout's one price rule, shown and charged alike. Each brand is
    priced at its position's volume discount (the 5th brand at the 5th
    position's tier, as on the public site), the yearly discount goes on top
    of the sum, and ``monthly_cents`` is rounded to a whole cent before the
    yearly charge is taken as twelve of it.

    A ``coupon`` for every charge comes off the monthly figure, so the yearly
    charge stays twelve of it; one for the first charge only leaves the
    recurring figures alone and lowers ``first_charge_cents``.
    """
    list_cents = workspace_price_cents()
    yearly = cycle == WorkspaceSubscription.Cycle.YEARLY
    annual_percent = annual_discount_percent() if yearly else 0
    volume_sum = sum(
        Decimal(list_cents) * (100 - volume_discount_percent(position)) / 100 for position in range(1, brands + 1)
    )
    monthly_cents = int((volume_sum * (100 - annual_percent) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    volume_cents = int(volume_sum.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    plain_monthly_cents = monthly_cents
    if coupon is not None and coupon.duration == Coupon.Duration.FOREVER:
        monthly_cents = _coupon_price(monthly_cents, coupon)
    charge_cents = monthly_cents * 12 if yearly else monthly_cents
    first_charge_cents = charge_cents
    if coupon is not None and coupon.duration == Coupon.Duration.FIRST:
        first_charge_cents = _coupon_price(charge_cents, coupon)
    plain_charge_cents = plain_monthly_cents * 12 if yearly else plain_monthly_cents
    return {
        "brands": brands,
        "cycle": WorkspaceSubscription.Cycle.YEARLY if yearly else WorkspaceSubscription.Cycle.MONTHLY,
        "list_cents": list_cents,
        "annual_percent": annual_percent,
        "monthly_cents": monthly_cents,
        "charge_cents": charge_cents,
        # What the card is charged today.
        "first_charge_cents": first_charge_cents,
        "coupon": coupon,
        # What the coupon takes off today's charge.
        "coupon_saving_cents": plain_charge_cents - first_charge_cents,
        # What the volume tiers take off the list price, per month.
        "volume_saving_cents": list_cents * brands - volume_cents,
        # Against paying the same brands month by month for a year.
        "yearly_saving_cents": (volume_cents - plain_monthly_cents) * 12 if yearly else 0,
    }


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------

#: Asaas refuses a charge under R$ 5,00, so no coupon takes a charge below it.
COUPON_MIN_CHARGE_CENTS = 500
COUPON_MAX_PERCENT = 100
_COUPON_CODE = re.compile(r"^[A-Z0-9_-]{3,32}$")


class CouponError(ValueError):
    """A coupon that cannot be used. The message is for the customer or the operator."""


def _coupon_price(cents, coupon):
    """``cents`` with ``coupon`` taken off, never below what Asaas will charge."""
    if coupon.kind == Coupon.Kind.PERCENT:
        off = int((Decimal(cents) * coupon.value / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    else:
        off = coupon.value
    return max(cents - off, min(cents, COUPON_MIN_CHARGE_CENTS))


def find_coupon(code, organization):
    """The usable coupon for ``code``, None when nothing was typed.

    Raises ``CouponError`` saying why a typed code cannot be used: unknown
    or switched off, past its date, used up, or already used by this
    organization.
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    coupon = Coupon.objects.filter(code=code, is_active=True).first()
    if coupon is None:
        raise CouponError(_("This coupon does not exist or is no longer valid."))
    if coupon.valid_until is not None and coupon.valid_until < timezone.localdate():
        raise CouponError(_("This coupon has expired."))
    if coupon.max_redemptions is not None and coupon.redemptions.count() >= coupon.max_redemptions:
        raise CouponError(_("This coupon has run out."))
    if coupon.redemptions.filter(organization=organization).exists():
        raise CouponError(_("Your account has already used this coupon."))
    return coupon


def redeem_coupon(coupon, organization, *, discount_cents):
    """Record that ``organization`` paid with ``coupon``. Once per organization."""
    redemption, _created = CouponRedemption.objects.get_or_create(
        coupon=coupon, organization=organization, defaults={"discount_cents": discount_cents}
    )
    return redemption


def create_coupon(data, *, created_by=None):
    """A coupon from the staff form's fields. Raises ``CouponError`` with what to fix.

    ``value`` is a whole percent for a percentage coupon and an amount in
    reais (``10,00``) for a fixed one; the amount comes off each month for
    a coupon on every charge, or once off the first charge.
    """
    code = (data.get("code") or "").strip().upper()
    if not _COUPON_CODE.match(code):
        raise CouponError(_("The code takes 3 to 32 letters, digits, hyphens or underscores."))
    if Coupon.objects.filter(code=code).exists():
        raise CouponError(_("There is already a coupon with this code."))
    kind = data.get("kind")
    if kind not in Coupon.Kind.values:
        raise CouponError(_("Choose a percentage or a fixed amount."))
    duration = data.get("duration")
    if duration not in Coupon.Duration.values:
        raise CouponError(_("Choose whether the coupon applies to the first charge or to every charge."))
    raw_value = (data.get("value") or "").strip()
    try:
        if kind == Coupon.Kind.PERCENT:
            value = _whole_number(raw_value, maximum=COUPON_MAX_PERCENT, message=_("Enter a percentage from 1 to 100."))
        else:
            value = _parse_currency_to_cents(raw_value, _COUPON_AMOUNT_MESSAGES)
        max_redemptions = _whole_number(
            data.get("max_redemptions"), maximum=1_000_000, message=_("The limit of uses must be a whole number.")
        )
    except PriceValidationError as exc:
        raise CouponError(str(exc)) from None
    if not value:
        raise CouponError(_("The discount must be more than zero."))
    valid_until = None
    if data.get("valid_until"):
        valid_until = parse_date(data["valid_until"])
        if valid_until is None:
            raise CouponError(_("Enter the last valid day as a date."))
    return Coupon.objects.create(
        code=code,
        kind=kind,
        value=value,
        duration=duration,
        valid_until=valid_until,
        max_redemptions=max_redemptions or None,
        note=(data.get("note") or "").strip()[:255],
        created_by=created_by,
    )


_COUPON_AMOUNT_MESSAGES = {
    "required": _("Enter the discount amount, in reais."),
    "not_a_number": _("The discount is not a valid amount. Enter it as 10,00."),
    "negative": _("The discount cannot be negative."),
    "too_precise": _("The discount cannot have fractions of a cent. Use at most two decimals."),
    "too_large": _("That discount is too high to be intentional. Check the amount."),
}


def plan_for(organization):
    """The organization's plan, or None."""
    return OrganizationPlan.objects.filter(organization=organization).first()


def _plan_eligible_rows(plan):
    """The organization's workspace rows a plan may cover, oldest workspace first.

    Comped workspaces cost nothing and workspaces paying on a subscription
    of their own are already paid for; neither takes one of the plan's brands.
    """
    from apps.workspaces.models import Workspace

    for workspace in Workspace.objects.filter(organization_id=plan.organization_id).order_by("created_at"):
        ensure_subscription(workspace)
    return list(
        WorkspaceSubscription.objects.filter(workspace__organization_id=plan.organization_id)
        .exclude(status=WorkspaceSubscription.Status.COURTESY)
        .filter(models.Q(gateway_subscription_id="") | models.Q(plan=plan))
        .select_related("workspace")
        .order_by("workspace__created_at")
    )


def sync_plan_coverage(plan):
    """Make the organization's workspace rows agree with its plan.

    An active plan covers its first ``brands`` eligible workspaces: their
    rows point at the plan and read ``active``, with the plan's period end.
    Rows the plan no longer covers (fewer brands bought) go back to
    ``pending``. A plan that is not active passes its status (past due,
    canceled) on to the rows it covers, which keep the link so a recovered
    payment reopens them. Returns the covered rows.
    """
    status = WorkspaceSubscription.Status
    with transaction.atomic():
        if not plan.is_active:
            covered = list(WorkspaceSubscription.objects.filter(plan=plan))
            target = plan.status if plan.status in (status.PAST_DUE, status.CANCELED) else status.PENDING
            for row in covered:
                row.status = target
                row.save(update_fields=["status", "updated_at"])
            return covered

        rows = _plan_eligible_rows(plan)
        covered, uncovered = rows[: plan.brands], rows[plan.brands :]
        for row in covered:
            row.plan = plan
            row.status = status.ACTIVE
            row.current_period_end = plan.current_period_end
            row.canceled_at = None
            row.save(update_fields=["plan", "status", "current_period_end", "canceled_at", "updated_at"])
        for row in uncovered:
            if row.plan_id == plan.pk:
                row.plan = None
                row.status = status.PENDING
                row.save(update_fields=["plan", "status", "updated_at"])
        return covered


def plan_has_room(plan):
    """Whether an active plan has a brand left for another workspace."""
    if plan is None or not plan.is_active:
        return False
    return WorkspaceSubscription.objects.filter(plan=plan).count() < plan.brands


# ---------------------------------------------------------------------------
# Payer identity — the organization's document and phone
# ---------------------------------------------------------------------------


def valid_tax_id(raw):
    """CPF or CNPJ digits from what was typed, or "" when it is not one."""
    digits = re.sub(r"\D", "", raw or "")
    if (len(digits) == 11 and is_valid_cpf(digits)) or (len(digits) == 14 and is_valid_cnpj(digits)):
        return digits
    return ""


def valid_phone(raw):
    """Brazilian phone digits (area code + number), or "" when it is not one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    return digits if len(digits) in (10, 11) else ""


def set_payer_identity(organization, *, tax_id, phone):
    """Record the organization's document and phone, given when it is named.

    They are what its invoices are issued to and what the free trial asks
    for, so they are collected once, with the organization's name.
    """
    profile = ensure_billing_profile(organization)
    profile.tax_id = tax_id
    profile.phone = phone
    profile.save(update_fields=["tax_id", "phone", "updated_at"])
    return profile


def payer_identity_known(organization):
    """Whether the organization's document and phone are on file."""
    profile = ensure_billing_profile(organization)
    return bool(profile.tax_id and profile.phone)


# ---------------------------------------------------------------------------
# Free trial
# ---------------------------------------------------------------------------


class TrialRefusedError(ValueError):
    """A free trial that must not start. The message is for the customer."""


def trial_days():
    """Length of the free trial in days. 0 = no trial is offered."""
    return _resolved_rule(ProductPrice.Key.TRIAL_DAYS)


def subscription_required():
    """Whether a workspace that is neither paid nor comped opens only its contracting page."""
    return bool(_resolved_rule(ProductPrice.Key.REQUIRE_SUBSCRIPTION))


def trial_available(workspace, user=None):
    """Whether this workspace may start a free trial.

    One trial per organization, for a workspace that was never contracted,
    and one per person: ``user`` gets none when an organization they own
    has already had its trial.
    """
    if trial_days() <= 0:
        return False
    sub = ensure_subscription(workspace)
    if sub.status != WorkspaceSubscription.Status.PENDING or sub.gateway_subscription_id or sub.trial_ends_at:
        return False
    profile = ensure_billing_profile(workspace.organization)
    if profile.trial_started_at is not None:
        return False
    if user is None:
        return True
    return not (
        OrganizationBillingProfile.objects.filter(
            trial_started_at__isnull=False,
            organization__memberships__user=user,
            organization__memberships__org_role="owner",
        )
        .exclude(pk=profile.pk)
        .exists()
    )


#: Mailbox providers anyone can open an address at. An address there says
#: nothing about its owner's company, so two of them sharing a domain are
#: not treated as the same customer.
FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "hotmail.com.br",
        "outlook.com",
        "outlook.com.br",
        "live.com",
        "live.com.br",
        "msn.com",
        "yahoo.com",
        "yahoo.com.br",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "uol.com.br",
        "bol.com.br",
        "terra.com.br",
        "ig.com.br",
        "globo.com",
        "globomail.com",
        "r7.com",
        "zipmail.com.br",
        "proton.me",
        "protonmail.com",
        "aol.com",
        "zoho.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
    }
)


def normalize_trial_email(email):
    """An address reduced to the mailbox it reaches.

    Lower case, without the ``+tag`` most providers ignore, and for Gmail
    without the dots it ignores too — so ``Ana.Silva+teste@gmail.com`` and
    ``anasilva@gmail.com`` count as the one inbox they are.
    """
    local, _at, domain = (email or "").strip().lower().rpartition("@")
    if not local or not domain:
        return ""
    local = local.split("+", 1)[0]
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}"


def _trial_already_taken(profile, *, tax_id, phone, email):
    """Whether another account has spent a trial with any of these."""
    # The second arm of each pair covers trials started before the
    # trial_* snapshots existed.
    taken = models.Q(trial_tax_id=tax_id) | models.Q(tax_id=tax_id, trial_started_at__isnull=False)
    taken |= models.Q(trial_phone=phone) | models.Q(phone=phone, trial_started_at__isnull=False)
    if email:
        taken |= models.Q(trial_email=email)
        domain = email.rpartition("@")[2]
        if domain not in FREE_EMAIL_DOMAINS:
            taken |= models.Q(trial_email__endswith=f"@{domain}")
    return OrganizationBillingProfile.objects.filter(taken).exclude(pk=profile.pk).exists()


def start_trial(workspace, *, tax_id, phone, email="", user=None):
    """Open the account's free trial on ``workspace``.

    ``tax_id`` (CPF/CNPJ digits), ``phone`` and ``email`` are what the
    trial costs: they identify the person, and the trial is refused when
    another account already took one with the same document, phone or
    email, or with an email at the same company domain. Returns the
    subscription.
    """
    email = normalize_trial_email(email)
    with transaction.atomic():
        # Locked so two submissions cannot both find the trial unused.
        profile = OrganizationBillingProfile.objects.select_for_update().get(
            pk=ensure_billing_profile(workspace.organization).pk
        )
        if not trial_available(workspace, user):
            raise TrialRefusedError(_("This account has already used its free trial."))
        if _trial_already_taken(profile, tax_id=tax_id, phone=phone, email=email):
            raise TrialRefusedError(
                _(
                    "A free trial has already been used with this document, phone or email. "
                    "Contract the plan to start, or get in touch with us."
                )
            )

        now = timezone.now()
        profile.tax_id = tax_id
        profile.phone = phone
        profile.trial_started_at = now
        profile.trial_tax_id = tax_id
        profile.trial_phone = phone
        profile.trial_email = email
        profile.save(
            update_fields=[
                "tax_id",
                "phone",
                "trial_started_at",
                "trial_tax_id",
                "trial_phone",
                "trial_email",
                "updated_at",
            ]
        )

        sub = ensure_subscription(workspace)
        sub.trial_ends_at = now + datetime.timedelta(days=trial_days())
        sub.save(update_fields=["trial_ends_at", "updated_at"])
    return sub


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def extra_storage_bytes(workspace_id):
    """Bytes granted to a workspace by its active storage add-ons.

    Summed in GB and multiplied once, rather than summing a per-row byte
    expression, to keep the arithmetic identical to
    ``WorkspaceAddon.storage_bytes`` on a single row.
    """
    gb = (
        WorkspaceAddon.objects.filter(
            workspace_id=workspace_id,
            kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
            status=WorkspaceAddon.Status.ACTIVE,
        ).aggregate(total=models.Sum("quantity"))["total"]
        or 0
    )
    return gb * WorkspaceAddon.BYTES_PER_GB


def total_storage_bytes(workspace):
    """A workspace's full disk allowance: base quota plus add-ons.

    Returns 0 for a workspace with no subscription row at all — no
    contract, no quota. That is the honest answer rather than falling
    back to the settings default, which would hand free disk to a
    workspace that never subscribed.
    """
    sub = _subscription_for(workspace)
    if sub is None:
        return 0
    return sub.base_storage_bytes + extra_storage_bytes(_workspace_id(workspace))


def addons_monthly_cents(workspace_id):
    """Monthly cost of a workspace's active add-ons, at sold prices."""
    total = 0
    for addon in WorkspaceAddon.objects.filter(
        workspace_id=workspace_id,
        status=WorkspaceAddon.Status.ACTIVE,
    ):
        total += addon.total_price_cents
    return total


def workspace_monthly_total_cents(workspace):
    """What a workspace bills per month, add-ons included."""
    sub = _subscription_for(workspace)
    if sub is None:
        return 0
    return sub.monthly_total_cents


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def is_workspace_active(workspace):
    """True when a workspace may be used.

    The single question the rest of the app should ask. A workspace with
    no subscription row is not active: every workspace, including the
    ones that predate billing, has to be contracted or comped.
    """
    sub = _subscription_for(workspace)
    return bool(sub and sub.is_active)


def is_subscription_current(workspace):
    """True when a workspace's money is in order right now.

    Stricter than :func:`is_workspace_active` — it also requires the paid
    period not to have lapsed. Meant for reconcile jobs and admin
    reporting, not for gating a page.
    """
    sub = _subscription_for(workspace)
    return bool(sub and sub.is_current)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def ensure_subscription(workspace):
    """Get or create the workspace's subscription row, priced from the
    current list.

    Created ``pending``: the row's existence means "we know what this
    workspace would cost", never "it is paid for". Callers that need the
    workspace usable must subscribe or comp it.
    """
    sub, _created = WorkspaceSubscription.objects.get_or_create(
        workspace=workspace,
        defaults={
            "price_cents": workspace_price_cents(),
            "base_storage_bytes": base_storage_bytes(),
        },
    )
    _cache_subscription(workspace, sub)
    return sub


def grant_courtesy(workspace, note="", granted_by=None):
    """Mark a workspace exempt from billing.

    Does not consult the organization's courtesy quota: the quota is an
    admin-facing budget, and an admin overriding their own budget for one
    workspace is a decision, not an error. :func:`courtesy_quota_exceeded`
    is there for the UI to surface it.
    """
    sub = ensure_subscription(workspace)
    sub.status = WorkspaceSubscription.Status.COURTESY
    sub.courtesy_note = note
    sub.canceled_at = None
    sub.save(update_fields=["status", "courtesy_note", "canceled_at", "updated_at"])
    if granted_by is not None:
        profile = ensure_billing_profile(workspace.organization)
        if profile.courtesy_granted_at is None:
            profile.courtesy_granted_at = timezone.now()
            profile.courtesy_granted_by = granted_by
            profile.save(update_fields=["courtesy_granted_at", "courtesy_granted_by", "updated_at"])
    return sub


def revoke_courtesy(workspace):
    """Take a workspace's exemption away, returning it to ``pending``.

    Back to pending rather than active: removing a comp means the
    workspace now needs a real contract, and only the gateway can say it
    has one.
    """
    sub = ensure_subscription(workspace)
    if sub.status == WorkspaceSubscription.Status.COURTESY:
        sub.status = WorkspaceSubscription.Status.PENDING
        sub.courtesy_note = ""
        sub.save(update_fields=["status", "courtesy_note", "updated_at"])
    return sub


def contract_storage_addon(workspace, quantity_gb, unit_price_cents=None):
    """Contract ``quantity_gb`` extra GB for a workspace.

    The price is snapshotted onto the row at contracting time, defaulting
    to the current list price. Pass ``unit_price_cents`` explicitly to
    honour a quote that was shown to the customer before the list moved.
    """
    return WorkspaceAddon.objects.create(
        workspace=workspace,
        kind=WorkspaceAddon.Kind.EXTRA_STORAGE,
        quantity=quantity_gb,
        unit_price_cents=(storage_price_per_gb_cents() if unit_price_cents is None else unit_price_cents),
    )


# ---------------------------------------------------------------------------
# Organization-level courtesy
# ---------------------------------------------------------------------------


def ensure_billing_profile(organization):
    profile, _created = OrganizationBillingProfile.objects.get_or_create(organization=organization)
    return profile


def grant_account_courtesy(organization, *, quota, expires_on=None, note="", granted_by=None):
    """Open (or extend) an account-wide exemption, optionally with a deadline.

    One call rather than the operator setting a ceiling and then toggling
    each workspace: the product owner's decision is "this account is free
    — this many workspaces, until this day, for this reason", and splitting
    it across two writes is how a quota ends up raised with nothing
    actually exempted.

    So this both records the budget *and* spends it, comping the account's
    cheapest-to-decide set — its oldest workspaces, up to ``quota``. Oldest
    first because an agency's first workspace is the one the courtesy was
    negotiated over; a later one they create is the one they would expect
    to pay for.

    Workspaces already comped are left alone, which keeps a re-grant (a
    renewal, say) idempotent instead of shuffling which workspace is free.
    """
    profile = ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = quota
    profile.courtesy_expires_on = expires_on
    profile.courtesy_note = note
    profile.courtesy_granted_at = timezone.now()
    profile.courtesy_granted_by = granted_by
    profile.save(
        update_fields=[
            "courtesy_workspace_quota",
            "courtesy_expires_on",
            "courtesy_note",
            "courtesy_granted_at",
            "courtesy_granted_by",
            "updated_at",
        ]
    )

    workspaces = list(organization.workspaces.order_by("created_at", "id"))
    comped = 0
    for workspace in workspaces:
        if comped >= quota:
            break
        sub = ensure_subscription(workspace)
        if sub.status != WorkspaceSubscription.Status.COURTESY:
            grant_courtesy(workspace, note=note)
        comped += 1
    return profile


def revoke_account_courtesy(organization):
    """Close an account's exemption and bill every workspace again.

    Used both by the operator pressing "charge this account again" and by
    the expiry job. Zeroes the budget and clears the deadline so the
    account does not sit at "0 workspaces free until the 30th", which
    reads as a comp that is still live.

    Changes billing state only: no workspace, membership, asset or login
    is touched. A lapsed courtesy is an invoice event, never a lockout —
    see :func:`expire_due_courtesies`.
    """
    profile = ensure_billing_profile(organization)
    profile.courtesy_workspace_quota = 0
    profile.courtesy_expires_on = None
    profile.save(update_fields=["courtesy_workspace_quota", "courtesy_expires_on", "updated_at"])

    for workspace in organization.workspaces.all():
        revoke_courtesy(workspace)
    return profile


def due_courtesy_profiles(*, today=None):
    """Profiles whose deadline has passed and are still exempt.

    ``courtesy_expires_on < today`` (strict), matching
    ``OrganizationBillingProfile.courtesy_is_expired`` — a comp granted
    "until the 30th" lasts all of the 30th.

    Filtered on ``quota > 0`` rather than on the workspace statuses so a
    second run the same day finds nothing: :func:`revoke_account_courtesy`
    zeroes the quota, which is what makes the sweep idempotent.
    """
    if today is None:
        today = timezone.localdate()
    return OrganizationBillingProfile.objects.filter(
        courtesy_expires_on__isnull=False,
        courtesy_expires_on__lt=today,
        courtesy_workspace_quota__gt=0,
    ).select_related("organization")


def expire_due_courtesies(*, today=None):
    """Put every lapsed account back on the invoice. Returns how many.

    What expiring means, exactly, and the whole reason this is a separate
    function rather than a line in the job: it flips billing state and
    nothing else. No workspace is archived, no asset is deleted, no
    member loses their login. A customer whose free period ended owes
    money — they have not done anything wrong, and waking up to a dead
    account would be the one failure mode worse than an unpaid invoice.

    Each account is committed on its own so one bad row cannot roll back
    the rest of the sweep.
    """
    expired = 0
    for profile in due_courtesy_profiles(today=today):
        with transaction.atomic():
            revoke_account_courtesy(profile.organization)
        expired += 1
    return expired


def courtesy_quota_exceeded(organization):
    """True when an org has more comped workspaces than its quota allows.

    Reported rather than enforced — see :func:`grant_courtesy`. Lets the
    admin UI show "4 of 3 courtesy workspaces in use" instead of
    silently refusing a grant an admin deliberately made.
    """
    profile = OrganizationBillingProfile.objects.filter(organization=organization).first()
    quota = profile.courtesy_workspace_quota if profile else 0
    used = WorkspaceSubscription.objects.filter(
        workspace__organization=organization,
        status=WorkspaceSubscription.Status.COURTESY,
    ).count()
    return used > quota


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------


def record_event(gateway_event_id, *, gateway="", event_type="", payload=None):
    """Claim a gateway event for processing.

    Returns ``(event, created)``. ``created=False`` means this event id
    was already recorded and the caller must not act on it again — the
    insert IS the claim, which is why the unique constraint does the
    work instead of a prior existence check that two concurrent
    deliveries would both pass.

    Wrapped in its own atomic block so the IntegrityError that
    ``get_or_create`` absorbs doesn't poison an outer transaction.
    """
    with transaction.atomic():
        event, created = BillingEvent.objects.get_or_create(
            gateway_event_id=gateway_event_id,
            defaults={
                "gateway": gateway,
                "event_type": event_type,
                "payload": payload or {},
            },
        )
    return event, created


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_workspace_id(workspace):
    return isinstance(workspace, str | uuid.UUID)


def _cache_subscription(workspace, sub):
    """Point a Workspace instance's cached reverse relation at ``sub``.

    Django caches the reverse OneToOne on first access, so a caller
    holding the Workspace it just passed to ``grant_courtesy`` would
    otherwise keep reading the pre-grant status (or a cached
    DoesNotExist) for the rest of the request.
    """
    if not _is_workspace_id(workspace):
        workspace.billing_subscription = sub


def _workspace_id(workspace):
    return workspace if _is_workspace_id(workspace) else workspace.pk


def _subscription_for(workspace):
    """The workspace's subscription, or None.

    Tolerates being handed either a Workspace or a bare id. Accessing
    the reverse side of a OneToOne raises rather than returning None
    when the row is absent, so the missing-subscription case — the
    normal state of every workspace that predates billing — is caught
    here instead of at each call site.
    """
    if _is_workspace_id(workspace):
        return WorkspaceSubscription.objects.filter(workspace_id=workspace).first()
    try:
        return workspace.billing_subscription
    except WorkspaceSubscription.DoesNotExist:
        return None
