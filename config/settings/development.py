import os as _os

from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# CSRF trust for HTTPS tunnels (ngrok, cloudflared, etc.) used during local
# Intelligence integration testing. Django requires the request Origin /
# Referer host to be explicitly trusted for any POST coming through a
# non-localhost hostname, even with DEBUG=True. Reads STUDIO_BASE_URL if
# set (the integration's https-required env var) so you don't have to
# remember a second env knob — anything else can go into CSRF_TRUSTED_ORIGINS
# explicitly via env. Wildcarded scheme: HTTPS only.
_csrf_trusted = []
_studio_base = _os.environ.get("STUDIO_BASE_URL", "").strip().rstrip("/")
if _studio_base.startswith("https://"):
    _csrf_trusted.append(_studio_base)
# Extra hosts (comma-separated) e.g. "https://foo.ngrok-free.app,https://bar"
_extra = _os.environ.get("CSRF_TRUSTED_ORIGINS", "").strip()
if _extra:
    _csrf_trusted.extend(o.strip() for o in _extra.split(",") if o.strip())
if _csrf_trusted:
    CSRF_TRUSTED_ORIGINS = _csrf_trusted

# Tunnel-aware redirect handling. ngrok terminates TLS and forwards plain
# HTTP to runserver; without this Django treats requests as http:// and
# Stripe's success URL redirect → activate view would build wrong scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Plain storage in dev — no manifest needed, runserver uses finders directly.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

# Console email in development by default. EMAIL_BACKEND stays the budget wrapper from
# base.py so the real send path is what runs locally, but the caps are off
# (negative = unlimited): the console backend exists to show you every message,
# and inheriting the production 6-per-recipient-per-hour would silently swallow
# the seventh with nothing but a log line — for the rest of the hour, since the
# counters are in the database and survive a restart. Set them to real values
# locally when the budget itself is what you are testing.
EMAIL_INNER_BACKEND = "django.core.mail.backends.console.EmailBackend"
# With DEV_SMTP_HOST set, messages go to a local mail catcher instead (Mailpit;
# docker-compose.override.yml starts one and points this at it), so signup,
# password reset and notification mails can be opened and clicked through. A
# name of its own on purpose: EMAIL_HOST in .env may be the real provider, and
# development must never deliver through it.
_dev_smtp_host = _os.environ.get("DEV_SMTP_HOST", "").strip()
if _dev_smtp_host:
    EMAIL_INNER_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = _dev_smtp_host
    EMAIL_PORT = int(_os.environ.get("DEV_SMTP_PORT", "1025"))
    EMAIL_HOST_USER = ""
    EMAIL_HOST_PASSWORD = ""
    EMAIL_USE_TLS = False
EMAIL_DAILY_SEND_LIMIT = -1
EMAIL_RECIPIENT_HOURLY_LIMIT = -1
EMAIL_RECIPIENT_DAILY_LIMIT = -1

# The CSP is enforced here as in production, so a script it blocks (an inline
# one without the page's nonce, say) fails locally instead of only online.
# CSP_REPORT_ONLY=true in .env turns it back into reports, e.g. to try a
# third-party script before adding its source to CSP_SCRIPT_SRC.
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", default=False)  # noqa: F405

# Django debug toolbar (optional)
try:
    import debug_toolbar  # noqa: F401

    INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
    INTERNAL_IPS = ["127.0.0.1"]
except ImportError:
    pass

SESSION_COOKIE_SECURE = False
