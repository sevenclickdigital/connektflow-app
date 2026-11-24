"""The affiliate's link and their own panel.

The link is public. The panel is for an account an operator made an
affiliate and switched on; anyone else gets a 404, so the programme is not
advertised to accounts that are not part of it.
"""

import functools

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.affiliates import emails, services
from apps.affiliates.models import Affiliate, Payout
from apps.common.periods import Period, parse_period

PAGE_SIZE = 20
#: The panel's tabs; the first is the one opened by default.
TABS = ("accounts", "commissions", "withdrawals")
PERIOD_CHOICES = (7, 30, 90)
REFERRAL_STATUSES = ("pending", "paying")


@require_GET
def link(request, slug):
    """Open an affiliate's link: count the visit, remember the affiliate, go to sign-up."""
    response = redirect("dashboard" if request.user.is_authenticated else "account_signup")
    affiliate = Affiliate.objects.filter(slug=slug.lower(), is_active=True).first()
    if affiliate is None or request.user.is_authenticated:
        return response
    services.remember_click(affiliate)
    response.set_signed_cookie(
        services.COOKIE_NAME,
        affiliate.slug,
        max_age=services.COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure(),
    )
    return response


def affiliate_required(view_func):
    """Log in, and be a switched-on affiliate; ``request.affiliate`` is set."""

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        affiliate = services.active_affiliate_for(request.user)
        if affiliate is None:
            raise Http404
        request.affiliate = affiliate
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


@affiliate_required
@require_GET
def panel(request):
    """The affiliate's numbers, the accounts they brought, their commissions and withdrawals.

    The period chosen bounds the numbers and the accounts and commissions
    lists; the balance and the withdrawals are always whole. ``tab`` picks the
    list shown, paged by ``page``; ``status`` narrows the accounts.
    """
    affiliate = request.affiliate
    today = timezone.localdate()
    period = parse_period(
        request.GET,
        choices=PERIOD_CHOICES,
        default=PERIOD_CHOICES[1],
        today=today,
        allow_all=True,
        fallback=Period(days=PERIOD_CHOICES[1], end=today, today=today, everything=True),
    )
    start, end = (None, None) if period.everything else (period.start, period.end)
    status = request.GET.get("status", "")
    if status not in REFERRAL_STATUSES:
        status = ""
    tab = request.GET.get("tab", "")
    if tab not in TABS:
        tab = TABS[0]
    numbers = services.numbers(affiliate, start=start, end=end)
    context = {
        "affiliate": affiliate,
        "period": period,
        "period_choices": PERIOD_CHOICES,
        "status": status,
        "tab": tab,
        "numbers": numbers,
        "link": request.build_absolute_uri(reverse("affiliate_link", kwargs={"slug": affiliate.slug})),
        "hold_days": services.COMMISSION_HOLD_DAYS,
        "payout_days": services.PAYOUT_DEADLINE_DAYS,
        "settings_active": "affiliate",
    }
    # Only the open tab's list is read, a page at a time.
    page_number = request.GET.get("page")
    if tab == "accounts":
        referrals = services.referrals_in(affiliate, start=start, end=end, status=status)
        context["page_obj"] = Paginator(referrals, PAGE_SIZE).get_page(page_number)
    elif tab == "commissions":
        context["page_obj"] = page = Paginator(
            services.commissions_in(affiliate, start=start, end=end), PAGE_SIZE
        ).get_page(page_number)
        for commission in page:
            commission.released_on = services.released_on(commission)
    else:
        context["page_obj"] = Paginator(affiliate.payouts.all(), PAGE_SIZE).get_page(page_number)
    return render(request, "affiliates/panel.html", context)


@affiliate_required
@require_POST
def request_payout(request):
    """Ask for the withdrawal, then tell the affiliate and the superadmins by e-mail."""
    try:
        payout = services.request_payout(request.affiliate, request.POST.get("pix_key"))
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect(_withdrawals_tab())
    messages.success(
        request,
        _("Withdrawal requested. We will pay it to the Pix key you entered within %(days)s days.")
        % {"days": services.PAYOUT_DEADLINE_DAYS},
    )
    emails.send_payout_requested(payout)
    emails.send_payout_requested_to_team(
        payout, review_url=request.build_absolute_uri(reverse("affiliates_staff:affiliates"))
    )
    return redirect(_withdrawals_tab())


def _withdrawals_tab():
    return f"{reverse('affiliates:panel')}?tab=withdrawals"


@affiliate_required
@require_GET
def payout_receipt(request, payout_id):
    """The receipt of one of the affiliate's own withdrawals."""
    return receipt_response(get_object_or_404(Payout, pk=payout_id, affiliate=request.affiliate))


def receipt_response(payout):
    """Serve ``payout``'s receipt; 404 when it has none or the file is gone."""
    if not payout.receipt:
        raise Http404
    try:
        handle = payout.receipt.open("rb")
    except Exception:  # noqa: BLE001 - storage backends raise varied errors (OSError, botocore ClientError)
        raise Http404 from None
    response = FileResponse(handle, filename=payout.receipt.name.rsplit("/", 1)[-1])
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response
