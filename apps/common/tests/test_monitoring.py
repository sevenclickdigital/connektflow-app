"""What reaches the error tracker, and the endpoints that feed and watch it."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from background_task.models import CompletedTask, Task
from background_task.tasks import Tasks
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.common.error_reporting import SecretScrubber
from apps.common.monitoring_views import CLIENT_ERRORS_PER_HOUR
from apps.common.tasks import purge_completed_tasks
from apps.common.worker import LANE_MAX_WAIT_SECONDS, LANES, late_lanes

pytestmark = pytest.mark.django_db

PUBLISH = "apps.publisher.tasks.run_publish_cycle"
SYNC = "apps.analytics.tasks.sync_all_account_analytics"
SWEEP = "apps.somewhere.tasks.sweep"


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    Task.objects.all().delete()
    CompletedTask.objects.all().delete()


@pytest.fixture
def registry():
    return Tasks()


def _queue(registry, name, *, waited: int, locked: bool = False):
    """A task of ``name`` that has been due for ``waited`` seconds."""
    registry.background(name=name)(lambda: None)
    task = Task.objects.new_task(name)
    task.run_at = timezone.now() - timedelta(seconds=waited)
    if locked:
        task.locked_by, task.locked_at = "sync-1", timezone.now()
    task.save()
    return task


def test_a_secret_is_masked_by_what_its_name_contains():
    """Tracebacks carry every frame's local variables; an account's token must not go with them."""
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "access_token": "EAAB-live",
                                    "page_access_token": "EAAB-page",
                                    "caption": "hello",
                                    "payload": {"client_secret": "shh", "media_ids": [1, 2]},
                                }
                            }
                        ]
                    }
                }
            ]
        },
        "request": {"headers": {"Authorization": "Bearer x", "Accept": "text/html"}},
    }

    SecretScrubber(recursive=True).scrub_event(event)

    sent = json.dumps(event, default=lambda _: "[masked]")
    assert "EAAB" not in sent and "shh" not in sent and "Bearer" not in sent
    variables = event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert variables["caption"] == "hello"
    assert variables["payload"]["media_ids"] == [1, 2]
    assert event["request"]["headers"]["Accept"] == "text/html"


def test_a_lane_is_late_once_a_due_task_waits_past_its_limit(registry):
    _queue(registry, PUBLISH, waited=LANE_MAX_WAIT_SECONDS["publishing"] + 60)
    _queue(registry, SYNC, waited=LANE_MAX_WAIT_SECONDS["publishing"] + 60)  # normal for the sync lane
    _queue(registry, SWEEP, waited=LANE_MAX_WAIT_SECONDS["general"] + 60)

    late = late_lanes(registry=registry)

    assert set(late) == {"publishing", "general"}
    assert late["publishing"] >= LANE_MAX_WAIT_SECONDS["publishing"]


def test_every_lane_has_a_wait_limit():
    assert set(LANE_MAX_WAIT_SECONDS) == set(LANES)


def test_a_task_being_run_does_not_make_its_lane_late(registry):
    _queue(registry, PUBLISH, waited=LANE_MAX_WAIT_SECONDS["publishing"] + 60, locked=True)

    assert late_lanes(registry=registry) == {}


def test_queue_health_answers_503_naming_the_late_lane(client):
    Task.objects.create(
        task_name=PUBLISH,
        task_params="[[], {}]",
        task_hash="h",
        run_at=timezone.now() - timedelta(seconds=LANE_MAX_WAIT_SECONDS["publishing"] + 60),
    )

    response = client.get(reverse("queue_health"))

    assert response.status_code == 503
    assert list(response.json()["lanes"]) == ["publishing"]


def test_queue_health_answers_200_when_every_lane_keeps_up(client):
    Task.objects.create(task_name=PUBLISH, task_params="[[], {}]", task_hash="h", run_at=timezone.now())

    response = client.get(reverse("queue_health"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_queue_health_reads_the_queue_once_per_cache_window(client):
    with patch("apps.common.monitoring_views._late_lanes", return_value={}) as read:
        client.get(reverse("queue_health"))
        client.get(reverse("queue_health"))

    assert read.call_count == 1


def _report(client, **body):
    return client.post(reverse("client_error"), data=json.dumps(body), content_type="application/json")


def test_a_browser_error_is_recorded_for_the_signed_in_user(client, user):
    client.force_login(user)

    with patch("apps.common.monitoring_views.capture_client_error") as capture:
        response = _report(
            client,
            kind="error",
            message="x is not defined",
            source="/static/js/app.js",
            line=12,
            stack="s" * 9000,
            page="/workspace/1/compose/",
            anything_else="dropped",
        )

    assert response.status_code == 204
    recorded = capture.call_args.kwargs
    assert recorded["kind"] == "error"
    assert recorded["message"] == "x is not defined"
    assert recorded["user_id"] == user.pk
    assert recorded["details"]["line"] == 12
    assert len(recorded["details"]["stack"]) == 4000
    assert "anything_else" not in recorded["details"]


def test_an_anonymous_visitor_cannot_report_errors(client):
    with patch("apps.common.monitoring_views.capture_client_error") as capture:
        response = _report(client, kind="error", message="boom")

    assert response.status_code == 403
    capture.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "made-up", "message": "boom"},
        {"kind": "error", "message": ""},
        {"kind": "error", "message": {"nested": "object"}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_report_is_refused(client, user, body):
    client.force_login(user)

    with patch("apps.common.monitoring_views.capture_client_error") as capture:
        response = client.post(reverse("client_error"), data=json.dumps(body), content_type="application/json")

    assert response.status_code == 400
    capture.assert_not_called()


def test_reports_past_the_hourly_limit_are_dropped(client, user):
    client.force_login(user)

    with patch("apps.common.monitoring_views.capture_client_error") as capture:
        for number in range(CLIENT_ERRORS_PER_HOUR + 5):
            response = _report(client, kind="error", message=f"boom {number}")

    assert response.status_code == 204
    assert capture.call_count == CLIENT_ERRORS_PER_HOUR


def _finished(*, days_ago: int, failed: bool = False):
    when = timezone.now() - timedelta(days=days_ago)
    return CompletedTask.objects.create(
        task_name=PUBLISH,
        task_params="[[], {}]",
        task_hash="h",
        run_at=when,
        failed_at=when if failed else None,
    ).pk


def test_finished_task_records_are_dropped_past_their_retention():
    recent = _finished(days_ago=2)
    old = _finished(days_ago=10)
    failure = _finished(days_ago=10, failed=True)
    old_failure = _finished(days_ago=40, failed=True)

    purge_completed_tasks.now()

    kept = set(CompletedTask.objects.values_list("pk", flat=True))
    assert kept == {recent, failure}
    assert old not in kept and old_failure not in kept
