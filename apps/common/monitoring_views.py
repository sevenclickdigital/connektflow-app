"""Endpoints the error tracker and its uptime monitor talk to."""

import json

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .error_reporting import CLIENT_ERROR_KINDS, capture_client_error
from .worker import discover_tasks, late_lanes

# The queue is read at most this often, however often the endpoint is hit.
QUEUE_HEALTH_CACHE_SECONDS = 30
# Reports one account may send per hour; a page stuck in an error loop, or
# someone posting by hand, stops being recorded past it.
CLIENT_ERRORS_PER_HOUR = 30
CLIENT_ERROR_MAX_BYTES = 20_000


def _late_lanes() -> dict[str, int]:
    discover_tasks()
    return late_lanes()


@require_GET
def queue_health(request):
    """200 while every lane keeps up with its due tasks, 503 naming the late ones.

    For an uptime monitor: a worker container that is down, hung or far behind
    shows here as tasks waiting past ``worker.LANE_MAX_WAIT_SECONDS``. Public
    like ``/health/``, so it answers from a short cache and says nothing but
    lane names and seconds.
    """
    late = cache.get_or_set("queue-health", _late_lanes, QUEUE_HEALTH_CACHE_SECONDS)
    if late:
        return JsonResponse({"status": "late", "lanes": late}, status=503)
    return JsonResponse({"status": "ok"})


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@require_POST
def client_error(request):
    """Record an error the signed-in user's browser reported (static/js/error-reporter.js).

    Always answers 204 to a signed-in user, recorded or not: the page has
    nothing to do with the outcome.
    """
    if not request.user.is_authenticated:
        return HttpResponse(status=403)
    if len(request.body) > CLIENT_ERROR_MAX_BYTES:
        return HttpResponse(status=413)
    try:
        data = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    kind = data.get("kind") if isinstance(data, dict) else None
    message = _text(data.get("message"), 300) if isinstance(data, dict) else ""
    if kind not in CLIENT_ERROR_KINDS or not message:
        return HttpResponse(status=400)

    counter = f"client-errors:{request.user.pk}"
    cache.add(counter, 0, 60 * 60)
    if cache.incr(counter) <= CLIENT_ERRORS_PER_HOUR:
        capture_client_error(
            kind=kind,
            message=message,
            user_id=request.user.pk,
            details={
                "source": _text(data.get("source"), 300),
                "line": _number(data.get("line")),
                "column": _number(data.get("column")),
                "stack": _text(data.get("stack"), 4000),
                "page": _text(data.get("page"), 300),
                "user_agent": request.headers.get("User-Agent", "")[:300],
            },
        )
    return HttpResponse(status=204)
