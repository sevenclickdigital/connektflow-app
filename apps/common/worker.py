"""One execution slot per kind of work, one container per lane in production.

Keep the queue library's row locks and task lifecycle, but not its shared
thread pool: two long background jobs could occupy every slot in that pool.

Each lane runs one task at a time and claims only its own tasks, so work of
one kind never waits behind another (``LANE_TASK_NAMES``):

- ``publishing``: the publish cycle and its confirmation, nothing else, so a
  scheduled post never waits behind another job;
- ``realtime``: automatic replies, a post's first comment and what fills in a
  message just received;
- ``inbox``: polling the inbox and importing or clearing its history;
- ``sync``: account syncs (analytics, token checks, webhooks, contacts, Radar);
- ``email``: e-mails, digests, notification retries and reminders;
- ``general``: everything not named above (sweeps, billing, deletions);
- ``media``: FFmpeg and Pillow over whole files (trims, uploads, image edits).

A lane is a thread, idle until it has work, and Django closes its database
connection after every task (``in_worker_thread``), so a lane costs a
connection only while it runs. With every lane busy at once the deployment
needs ``MIN_DATABASE_CONNECTIONS`` (apps/publisher/test_connection_budget.py
holds the sum to it); the worker logs an error at startup when the database
grants fewer (``warn_if_too_few_connections``).

``scope`` picks the lanes a process runs: one lane by name (``publishing``,
``media``...) or ``all`` of them (the default, development). Production runs
one container per lane (docker-compose.dokploy.yml), so a crash, an
out-of-memory kill or a hung dispatcher stops one kind of work and never the
others; the media one also runs under a CPU and memory cap.

A watchdog thread ends the process when the dispatcher stops going round or a
task outlives ``MAX_RUN_TIME`` (``StudioWorker.stall_reason``), so the
container's restart policy brings a stuck worker back instead of it looking
alive while nothing runs.
"""

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from background_task.models import Task
from background_task.settings import app_settings
from background_task.tasks import Tasks, bg_runner, tasks
from django.db import OperationalError, close_old_connections, connection, connections
from django.utils import timezone

from apps.common.db import in_worker_thread

logger = logging.getLogger(__name__)

PUBLISHING_TASK_NAMES = frozenset(
    {"apps.publisher.tasks.run_publish_cycle", "apps.publisher.tasks.confirm_pending_publishes"}
)
# What someone is waiting on right now: a reply to the comment or message
# they just sent, the first comment under a post that just went out. Not in
# "publishing": a batch of first comments after a busy cycle would hold up the
# next one, and with it the posts due.
REALTIME_TASK_NAMES = frozenset(
    {
        "apps.automations.tasks.run_automation_for_message",
        "apps.publisher.engine._post_first_comment_task",
        "apps.inbox.tasks.fill_instagram_mention",
    }
)
INBOX_TASK_NAMES = frozenset(
    {
        "apps.inbox.tasks.run_inbox_sync_cycle",
        "apps.inbox.tasks.import_inbox_history",
        "apps.inbox.tasks.clear_inbox_history",
    }
)
# Long, per-account calls to the platforms; none has someone waiting on it.
SYNC_TASK_NAMES = frozenset(
    {
        "apps.analytics.tasks.sync_all_account_analytics",
        "apps.analytics.tasks.backfill_account_analytics",
        "apps.analytics.tasks.retry_youtube_post_analytics",
        "apps.social_accounts.tasks.schedule_all_health_checks",
        "apps.social_accounts.tasks.check_social_account_health",
        "apps.social_accounts.webhooks.subscribe_account_webhooks_task",
        "apps.social_accounts.youtube_compliance.retry_youtube_revocations",
        "apps.social_accounts.youtube_compliance.enforce_youtube_retention",
        "apps.contacts.tasks.refresh_contacts",
        "apps.radar.tasks.collect_market_hashtags",
        "apps.intelligence.tasks.reconcile_intelligence_subscriptions",
        "apps.intelligence.tasks._refresh_one_subscription",
        "apps.intelligence.tasks.provision_intelligence_account_via_session",
    }
)
EMAIL_TASK_NAMES = frozenset(
    {
        "apps.email_center.tasks.send_automated_emails",
        "apps.notifications.tasks.send_batched_email_digests",
        "apps.notifications.tasks.retry_failed_deliveries",
        "apps.tasks.tasks.send_task_due_reminders",
        "apps.approvals.tasks.run_approval_reminders_cycle",
    }
)
# The jobs that run FFmpeg or Pillow over a whole file.
MEDIA_TASK_NAMES = frozenset(
    {
        "apps.media_library.tasks.trim_video_copy",
        "apps.media_library.tasks.process_media_asset",
        "apps.media_library.tasks.process_image_edit",
    }
)
# Every named lane, in the order the dispatcher offers them a task; "general"
# (whatever none of them names) comes last.
LANE_TASK_NAMES: dict[str, frozenset[str]] = {
    "publishing": PUBLISHING_TASK_NAMES,
    "realtime": REALTIME_TASK_NAMES,
    "inbox": INBOX_TASK_NAMES,
    "sync": SYNC_TASK_NAMES,
    "email": EMAIL_TASK_NAMES,
    "media": MEDIA_TASK_NAMES,
}
LANES = (*LANE_TASK_NAMES, "general")
WORKER_SCOPES = ("all", *LANES)
# Connections the whole deployment may hold at its peak: web threads, every
# worker container and the publish engine's pools (test_connection_budget.py).
MIN_DATABASE_CONNECTIONS = 30

# How long the dispatcher may go without finishing a round before the
# watchdog takes it for hung. A round is a few short queries plus a wait of
# ``poll_interval``, so minutes without one means it is stuck, not busy.
DISPATCHER_STALL_SECONDS = 300
WATCHDOG_INTERVAL_SECONDS = 30

# Set when the process receives SIGINT/SIGTERM. In-flight work may read it to
# cut a long wait short and leave the job retryable, rather than be killed when
# the platform's shutdown grace period (30s on Heroku) runs out.
SHUTDOWN_REQUESTED = threading.Event()


def lanes_for(scope: str) -> dict[str, frozenset[str] | None]:
    """The lanes a worker of ``scope`` runs: name -> its task names, None for "general"."""
    if scope not in WORKER_SCOPES:
        raise ValueError(f"Unknown worker scope {scope!r}")
    every: dict[str, frozenset[str] | None] = {**LANE_TASK_NAMES, "general": None}
    return every if scope == "all" else {scope: every[scope]}


def database_connection_limit() -> int | None:
    """How many connections this database role may open, or None if unknown.

    The smaller of the server's ``max_connections`` (less the slots reserved
    for superusers) and the role's own ``rolconnlimit`` when it sets one.
    """
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int,"
            " (SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user)"
        )
        server, role = cursor.fetchone()
    return min(server, role) if role and role > 0 else server


def warn_if_too_few_connections() -> None:
    """Log an error when the database grants fewer than ``MIN_DATABASE_CONNECTIONS``.

    Every lane busy at once, with the web threads and the publish engine's
    pools, would then run out of connections ("too many connections for
    role") and every process would lose the database together. Nothing is
    changed here: the remedy is a database with more connections.
    """
    try:
        limit = database_connection_limit()
    except Exception:
        logger.exception("Studio worker could not read the database connection limit")
        return
    if limit is not None and limit < MIN_DATABASE_CONNECTIONS:
        logger.error(
            "The database grants %s connections; this deployment needs %s at its peak. "
            "Raise max_connections (or the role's limit).",
            limit,
            MIN_DATABASE_CONNECTIONS,
        )


class StudioWorker:
    """Claim at most one task per lane; never queue work behind a busy slot."""

    def __init__(
        self,
        *,
        duration: float = 0,
        poll_interval: float = 1.0,
        registry: Tasks | None = None,
        scope: str = "all",
    ) -> None:
        self.scope = scope
        self.lane_tasks = lanes_for(scope)
        self.lanes = tuple(self.lane_tasks)
        # What "general" leaves to the named lanes, whichever process runs
        # them: a media job is never run by the general worker.
        self._named_task_names = frozenset().union(*LANE_TASK_NAMES.values())
        self.duration = duration
        self.poll_interval = poll_interval
        self.registry = tasks if registry is None else registry
        self.stop_requested = threading.Event()
        self.wakeup = threading.Event()
        self.recycling = False
        self.active: dict[str, Future[None]] = {}
        # When each active lane's task started, and when the dispatcher last
        # finished a round (time.monotonic()), for the watchdog.
        self.task_started: dict[str, float] = {}
        self.last_round = time.monotonic()
        # Prefixed with the scope: every container's worker is PID 1, and the
        # startup unlock below releases every lock bearing this name, so a
        # shared name would have one lane's worker release another's live tasks.
        self.worker_name = str(os.getpid()) if scope == "all" else f"{scope}-{os.getpid()}"

    def request_stop(self) -> None:
        """Stop admission, without unlocking or interrupting in-flight tasks."""
        self.stop_requested.set()
        self.wakeup.set()

    def release_own_stale_locks(self) -> int:
        """Free tasks still locked under *this* process's name. Returns the count.

        A task that is killed mid-run — a deploy, an OOM, a `docker stop` —
        keeps its lock row, and ``Task.objects.unlocked()`` hides it until
        ``MAX_RUN_TIME`` expires. That default is 3600s, so one badly-timed
        deploy silently parks a five-minute job for a full hour. That is exactly
        how the inbox sync stopped ingesting comments twice in one afternoon,
        with the worker otherwise healthy and no error anywhere.

        **Scoped to this worker's own name, and only at startup.** The name is
        the PID (``worker_name``), and in a container the entrypoint is reliably
        PID 1 — so a row reading ``locked_by="1"`` on a process that has just
        started and holds nothing is, with certainty, the corpse of the
        container that preceded this one. That certainty is what makes the
        unlock safe where lowering ``MAX_RUN_TIME`` globally would not be: the
        same worker runs the publish cycle, and unlocking a publish task that is
        genuinely still running risks posting twice (see the note on
        ``analytics.tasks._RUN_BUDGET``). Here nothing of ours is running yet,
        so there is no live task to race.

        Rows locked by a *different* name are left alone: they belong to
        another lane's worker that may well be alive.
        """
        released = Task.objects.filter(locked_by=self.worker_name, locked_at__isnull=False).update(
            locked_by=None, locked_at=None
        )
        if released:
            logger.warning(
                "Studio worker released %s stale task lock(s) left by a previous process worker_name=%s",
                released,
                self.worker_name,
            )
        return released

    def _claim(self, lane: str) -> Task | None:
        now = timezone.now()
        # Build an unsliced query so lane selection precedes limiting. In
        # particular, do not use find_available()'s global async-slot count.
        available = Task.objects.unlocked(now).filter(
            run_at__lte=now,
            failed_at__isnull=True,
            task_name__in=list(self.registry._tasks),
        )
        names = self.lane_tasks[lane]
        if names is None:
            available = available.exclude(task_name__in=self._named_task_names)
        else:
            available = available.filter(task_name__in=names)
        available = available.order_by(f"{app_settings.BACKGROUND_TASK_PRIORITY_ORDERING}priority", "run_at", "pk")
        for candidate in available[:5]:
            claimed = candidate.lock(self.worker_name)
            if claimed is not None:
                return claimed
        return None

    def _execute(self, lane: str, task: Task) -> None:
        started = time.monotonic()
        task_id = task.pk  # bg_runner deletes the Task and clears its pk.
        delay = max(0.0, (timezone.now() - task.run_at).total_seconds())
        logger.info(
            "Studio task started lane=%s task=%s id=%s dispatch_delay_seconds=%.3f",
            lane,
            task.task_name,
            task_id,
            delay,
        )
        try:
            # bg_runner is synchronous even if the legacy worker's async
            # setting is enabled. It writes CompletedTask, repeats on success,
            # and reschedules failures, just as process_tasks does.
            bg_runner(self.registry._tasks[task.task_name], task)
        finally:
            logger.info(
                "Studio task finished lane=%s task=%s id=%s duration_seconds=%.3f",
                lane,
                task.task_name,
                task_id,
                time.monotonic() - started,
            )

    def stall_reason(self, now: float) -> str | None:
        """Why this worker counts as stuck at ``now`` (monotonic), or None.

        The dispatcher has not finished a round in ``DISPATCHER_STALL_SECONDS``,
        or a task has run past ``MAX_RUN_TIME`` — the age at which the queue
        library already takes its lock for abandoned and would hand it out
        again, so ending the process then retries nothing it would not.
        """
        if now - self.last_round > DISPATCHER_STALL_SECONDS:
            return f"dispatcher idle for {now - self.last_round:.0f}s"
        for lane, started in list(self.task_started.items()):
            if now - started > app_settings.BACKGROUND_TASK_MAX_RUN_TIME:
                return f"task on lane {lane} running for {now - started:.0f}s"
        return None

    def _watchdog(self) -> None:
        while not self.stop_requested.wait(WATCHDOG_INTERVAL_SECONDS):
            reason = self.stall_reason(time.monotonic())
            if reason:
                logger.critical("Studio worker stuck (%s); exiting so the container restarts", reason)
                logging.shutdown()
                # Not sys.exit: the hung thread would keep the process alive.
                os._exit(1)

    def _collect_finished(self) -> None:
        for lane, future in list(self.active.items()):
            if future.done():
                del self.active[lane]
                self.task_started.pop(lane, None)
                try:
                    future.result()
                except Exception:
                    # A failure outside the task body (e.g. persisting its
                    # result) retains the library's lock for stale recovery.
                    logger.exception("Studio task runner failed lane=%s", lane)

    def run(self) -> None:
        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=len(self.lanes), thread_name_prefix="studio-worker")
        logger.info(
            "Studio worker started scope=%s lanes=%s duration_seconds=%s",
            self.scope,
            ",".join(self.lanes),
            self.duration,
        )
        # Before claiming anything: nothing of ours is running yet, so a lock
        # bearing our own name can only be a previous process's corpse. See
        # ``release_own_stale_locks``.
        warn_if_too_few_connections()
        try:
            self.release_own_stale_locks()
        except OperationalError:
            # A worker that cannot reach the database here will fail again in
            # the claim loop below, where the existing retry handling lives.
            # Blocking startup on this would turn a recoverable blip into a
            # worker that never comes up.
            logger.exception("Studio worker could not release stale task locks at startup")
        threading.Thread(target=self._watchdog, name="studio-worker-watchdog", daemon=True).start()
        try:
            while not self.stop_requested.is_set():
                self.last_round = time.monotonic()
                self.wakeup.clear()
                self._collect_finished()
                if self.duration > 0 and time.monotonic() - started >= self.duration:
                    if not self.recycling:
                        logger.info("Studio worker recycling: draining non-publishing work while publishing continues")
                    self.recycling = True

                # At the hourly boundary, stop starting general work first.
                # Keep publishing until that long-running job is done; otherwise
                # recycling itself would reintroduce a 30-minute publish delay.
                others_drained = self.recycling and not any(lane != "publishing" for lane in self.active)
                if others_drained and "publishing" not in self.active:
                    break

                try:
                    close_old_connections()
                    for lane in self.lanes:
                        if self.stop_requested.is_set():
                            break
                        if lane in self.active or (self.recycling and (lane != "publishing" or others_drained)):
                            continue
                        task = self._claim(lane)
                        if task is not None:
                            self.task_started[lane] = time.monotonic()
                            self.active[lane] = executor.submit(in_worker_thread, self._execute, lane, task)
                            self.active[lane].add_done_callback(lambda _: self.wakeup.set())
                except OperationalError:
                    logger.exception("Studio worker could not claim tasks; retrying after the poll interval")
                    connections.close_all()

                # Short health/maintenance jobs can finish in milliseconds.
                # Wake on completion instead of charging each one a full poll
                # interval, while still checking for due publishing every second.
                self.wakeup.wait(self.poll_interval)
        finally:
            # SIGTERM stops claims immediately. A platform may forcibly kill
            # the process after its grace period; never unlock unfinished jobs
            # early, because the platform may already have accepted a publish.
            executor.shutdown(wait=True)
            self._collect_finished()
            connections.close_all()
            logger.info("Studio worker stopped")
