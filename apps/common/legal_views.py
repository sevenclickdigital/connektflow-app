"""Public legal pages.

Meta's App Review requires a Privacy Policy URL and a data-deletion URL that
both resolve anonymously. These views therefore take no login decorator — App
Review fetches them with no session.

The privacy policy and the terms are written on the public site
(connektflow-site, ``src/pages/privacidade.astro`` and ``termos.astro``); the
app's own addresses redirect there, so links in sent emails keep working. The
data-deletion page stays here: Meta posts its signed deletion requests to it,
and it shows their status from the database.
"""

from django.conf import settings
from django.http import HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# Written as the Meta app's display name: App Review compares the legal pages
# with it, so the two change together.
APP_NAME = "Connekt Flow"

PRIVACY_POLICY_URL = "https://connektflow.com.br/privacidade/"
TERMS_OF_SERVICE_URL = "https://connektflow.com.br/termos/"


def contact_email() -> str:
    """The address people write to, on the legal pages and in every email footer.

    DEFAULT_FROM_EMAIL is often a noreply address, so an explicit contact wins.
    Keep in step with the Meta app's "Email de contato": App Review compares the
    two, and a mismatch reads as an inconsistent policy.
    """
    return getattr(settings, "LEGAL_CONTACT_EMAIL", "") or "contato@connektflow.com.br"


def _context():
    return {
        "app_name": APP_NAME,
        "contact_email": contact_email(),
        "privacy_url": PRIVACY_POLICY_URL,
    }


def privacy_policy(request):
    """The Privacy Policy, on the public site."""
    return HttpResponsePermanentRedirect(PRIVACY_POLICY_URL)


@csrf_exempt
@require_http_methods(["GET", "HEAD", "POST"])
def data_deletion(request):
    """Public data-deletion instructions, required by Meta App Review.

    With ``?code=``, also the status of a deletion a platform asked for on
    someone's behalf: the link its data-deletion callback hands back.

    A POST is that callback itself: the Meta app has this address as its data
    deletion callback URL, so the request Facebook signs arrives here. Hence
    CSRF-exempt; the signature is checked by the callback.
    """
    if request.method == "POST":
        from apps.social_accounts.platform_callbacks import facebook_data_deletion

        return facebook_data_deletion(request)
    context = _context()
    code = request.GET.get("code", "").strip()
    if code:
        from apps.social_accounts.models import DataDeletionRequest

        context["deletion_code"] = code
        context["deletion_request"] = DataDeletionRequest.objects.filter(confirmation_code=code).first()
    return render(request, "legal/data_deletion.html", context)


def terms_of_service(request):
    """The Terms of Service, on the public site."""
    return HttpResponsePermanentRedirect(TERMS_OF_SERVICE_URL)


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
