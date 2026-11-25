"""Reading Graph API error bodies, shared by the Meta providers.

Graph answers a refused call with ``{"error": {...}}``: ``code`` and
``error_subcode`` say what happened, ``is_transient`` whether trying again can
help, and ``error_user_title`` / ``error_user_msg`` are the sentence Meta wrote
for a person, already in their language when the token's locale allows.
Codes ``190`` (invalid or expired token) and ``102`` (session) mean signing in
again; the throttling codes mean waiting. Those two families keep their own
routing (reconnect, back off) and are never read as a permanent refusal here.
"""

from __future__ import annotations

from .exceptions import APIError

TOKEN_ERROR_CODES = frozenset({102, 190})
# The app (4), the user (17), the Page (32), a custom limit (613) and the
# per-business limits for Pages and Instagram (800xx).
THROTTLE_ERROR_CODES = frozenset({4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80014})


def graph_error(body) -> dict:
    """The ``error`` object of a Graph response body, or {}."""
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def graph_error_message(body) -> str:
    """The sentence Meta wrote for the person, or its technical message."""
    error = graph_error(body)
    user_title = str(error.get("error_user_title") or "").strip()
    user_msg = str(error.get("error_user_msg") or "").strip()
    if user_msg:
        return f"{user_title}: {user_msg}" if user_title else user_msg
    return str(error.get("message") or "").strip()


def graph_user_message(body) -> str:
    """Only the sentence Meta wrote for the person ("" when it wrote none)."""
    error = graph_error(body)
    return graph_error_message(body) if str(error.get("error_user_msg") or "").strip() else ""


def is_permanent_graph_error(body) -> bool:
    """Whether Meta says no retry of this request can succeed.

    ``is_transient: false`` is that statement (an Instagram Story video over
    the length Meta takes is refused with it, subcode ``2207032`` "try
    re-creating the media"). Token and throttling codes are left out: a refresh
    or a wait can still fix them, and they have routing of their own.
    """
    error = graph_error(body)
    code = error.get("code")
    return error.get("is_transient") is False and code not in TOKEN_ERROR_CODES and code not in THROTTLE_ERROR_CODES


def permanent_graph_api_error(provider, response):
    """The ``APIError`` to raise for a refusal Meta calls permanent, or None.

    Worded like ``SocialProvider._error_for_response`` (the body stays in the
    message for the publish log) and still an ``APIError`` with its
    ``status_code``, as that method's contract requires; only ``retryable`` and
    ``user_message`` differ.
    """
    body = provider._safe_json(response)
    if response.status_code == 429 or not is_permanent_graph_error(body):
        return None
    return APIError(
        f"{provider.platform_name} API error {response.status_code}: {response.text[:500]}",
        status_code=response.status_code,
        platform=provider.platform_name,
        raw_response=body,
        retryable=False,
        user_message=graph_user_message(body),
    )
