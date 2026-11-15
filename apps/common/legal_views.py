"""Public legal pages.

Meta's App Review requires a Privacy Policy URL and a data-deletion URL that
both resolve anonymously. These views therefore take no login decorator and
read nothing from the database — App Review fetches them with no session.
"""

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render

# Shown in the page header. Static rather than auto-updated: the date should
# change when the text changes, not on every deploy.
LAST_UPDATED = "2 de novembro de 2026"

# Written as the Meta app's display name: App Review compares the legal pages
# with it, so the two change together.
APP_NAME = "Connekt Flow"


def _context():
    return {
        "app_name": APP_NAME,
        "last_updated": LAST_UPDATED,
        # DEFAULT_FROM_EMAIL is often a noreply address, so prefer an explicit
        # contact when one is configured.
        # Keep in step with the Meta app's "Email de contato": App Review
        # compares the two, and a mismatch reads as an inconsistent policy.
        "contact_email": getattr(settings, "LEGAL_CONTACT_EMAIL", "") or "robison@sevenclick.com.br",
    }


def privacy_policy(request):
    """Public Privacy Policy, linked from the Meta app configuration."""
    return render(request, "legal/privacy_policy.html", _context())


def data_deletion(request):
    """Public data-deletion instructions, required by Meta App Review.

    With ``?code=``, also the status of a deletion a platform asked for on
    someone's behalf: the link its data-deletion callback hands back.
    """
    context = _context()
    code = request.GET.get("code", "").strip()
    if code:
        from apps.social_accounts.models import DataDeletionRequest

        context["deletion_code"] = code
        context["deletion_request"] = DataDeletionRequest.objects.filter(confirmation_code=code).first()
    return render(request, "legal/data_deletion.html", context)


def terms_of_service(request):
    """Public Terms of Service, linked from the Meta app configuration."""
    return render(request, "legal/terms_of_service.html", _context())


# Meta reads the legal pages, and every image or video a post publishes, with
# its own crawlers, and reports a site without an explicit rule for them as
# "blocked by robots.txt". Everyone else keeps what having no robots.txt
# meant: nothing is disallowed (the app itself sits behind the login).
ROBOTS_TXT = """\
User-agent: facebookexternalhit
Allow: /

User-agent: Facebot
Allow: /

User-agent: meta-externalagent
Allow: /

User-agent: *
Disallow:
"""


def robots_txt(request):
    """The site's robots.txt."""
    return HttpResponse(ROBOTS_TXT, content_type="text/plain; charset=utf-8")
