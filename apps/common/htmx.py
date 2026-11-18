"""Small HTMX response helpers shared across apps."""

import json

from django.http import HttpResponse


def trigger_response(triggers, status=204):
    """Return an empty response that fires the given ``HX-Trigger`` events.

    ``triggers`` maps event name → detail, e.g.
    ``{"showToast": {...}, "approvalAction": True}``.
    """
    return HttpResponse(status=status, headers={"HX-Trigger": json.dumps(triggers)})


def toast_response(*, tone, title, body="", events=None):
    """204 that shows a client toast and optionally fires extra HX-Trigger events.

    The approval surfaces listen for ``showToast`` (renders the toast) plus a
    refresh event (e.g. ``approvalAction`` / ``portalAction``) to re-fetch their
    list. ``events`` is merged into the trigger payload.
    """
    triggers = {"showToast": {"tone": tone, "title": title, "body": body}}
    if events:
        triggers.update(events)
    return trigger_response(triggers)


def lazy_body_urls(request, part: str = "body") -> dict[str, str]:
    """For a page whose slow part loads after it shows (components/_skeleton.html).

    ``body_url`` is where the placeholder fetches that part from (the page's own
    URL with ``partial=<part>``); ``page_url`` is the page itself, what a "Try
    again" inside the loaded part goes back to. Both keep the page's other query
    parameters (range, filters).
    """
    query = request.GET.copy()
    query.pop("partial", None)
    page_url = f"{request.path}?{query.urlencode()}" if query else request.path
    query["partial"] = part
    return {"page_url": page_url, "body_url": f"{request.path}?{query.urlencode()}"}


def wants_lazy_body(request) -> bool:
    """The placeholder asking for the page's slow part (see :func:`lazy_body_urls`)."""
    return bool(request.headers.get("HX-Request")) and request.GET.get("partial") == "body"
