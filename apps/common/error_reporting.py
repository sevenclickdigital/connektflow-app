"""What this deployment sends to its error tracker, and how it is kept safe.

The tracker speaks the Sentry protocol (production runs GlitchTip), so
``sentry_sdk`` is the client. Everything is off without ``SENTRY_DSN``.

Imported by the settings module: keep Django out of the module level.
"""

import logging
from typing import Literal

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
from sentry_sdk.utils import AnnotatedValue

# A variable, header or field whose name contains one of these never leaves
# the process. The SDK's own list matches whole names only, which lets
# ``access_token``, ``page_access_token`` or ``client_secret`` through - and
# the event carries the local variables of every frame of the traceback.
SECRET_NAME_PARTS = ("token", "secret", "password", "passwd", "credential", "authorization", "cookie", "api_key")

# What a browser may report (``capture_client_error``), with the level it gets.
CLIENT_ERROR_KINDS: dict[str, Literal["error", "warning"]] = {
    "error": "error",  # an uncaught JavaScript error
    "rejection": "error",  # an unhandled promise rejection
    "resource": "error",  # a script or stylesheet that failed to load
    "htmx": "error",  # a swap HTMX could not do (target missing, swap failed)
    "freeze": "warning",  # the page stopped responding for seconds
}


class SecretScrubber(EventScrubber):
    """The SDK's scrubber, matching a secret's name by what it contains."""

    def scrub_dict(self, d: object) -> None:
        if not isinstance(d, dict):
            return
        for key, value in d.items():
            name = key.lower() if isinstance(key, str) else ""
            if name in self.denylist or any(part in name for part in SECRET_NAME_PARTS):
                d[key] = AnnotatedValue.substituted_because_contains_sensitive_data()
            elif self.recursive:
                self.scrub_dict(value)
                self.scrub_list(value)


def init(
    dsn: str,
    *,
    environment: str,
    traces_sample_rate: float,
    profiles_sample_rate: float,
    logs_level: str,
) -> None:
    """Start reporting to the tracker at ``dsn``.

    Errors (uncaught exceptions and ``logger.error``) become issues.
    ``logs_level`` names the level from which log lines are also kept as
    searchable logs, which is what survives a deploy (``docker logs`` does
    not); "" keeps none. ``traces_sample_rate`` is the share of requests and
    background tasks timed for the performance view.
    """
    level = logging.getLevelName(logs_level.upper()) if logs_level else None
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        profiles_sample_rate=profiles_sample_rate,
        # GlitchTip has no sessions; the SDK would send one per request.
        auto_session_tracking=False,
        enable_logs=isinstance(level, int),
        integrations=[LoggingIntegration(sentry_logs_level=level if isinstance(level, int) else None)],
        event_scrubber=SecretScrubber(denylist=DEFAULT_DENYLIST, recursive=True),
    )
    # Every scanner probing the server with a foreign Host header logs an
    # error here; none of them is ours to fix.
    ignore_logger("django.security.DisallowedHost")
    tag_service("app")


def tag_service(name: str) -> None:
    """Mark everything this process reports with the service it is ("app", "worker-sync"...)."""
    sentry_sdk.get_global_scope().set_tag("service", name)


def capture_client_error(*, kind: str, message: str, user_id: object, details: dict[str, object]) -> None:
    """Record an error a browser reported, as its own issue per distinct error.

    Grouped by kind, message and where it was raised, so one broken button is
    one issue however many people hit it.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_user({"id": str(user_id)})
        scope.set_tag("source", "browser")
        scope.set_tag("kind", kind)
        sentry_sdk.capture_event(
            {
                "level": CLIENT_ERROR_KINDS[kind],
                "logger": "browser",
                "message": message,
                "extra": details,
                "fingerprint": ["browser", kind, message, str(details.get("source", "")), str(details.get("line", ""))],
            }
        )
