"""Post Composer models (F-2.1) - core content creation entities.

Models:
    ContentCategory - Content categories for posts (e.g., Educational, Promotional).
    Idea - A content idea on the Kanban board, scoped to a workspace.
    Post - The base content entity, scoped to a workspace.
    PlatformPost - Per-platform variant of a post (caption/media overrides).
    PostMedia - Media attachments with ordering and alt text.
    PostVersion - Immutable snapshots for version history.
    PostTemplate - Reusable post templates.
    CSVImportJob - Tracks bulk CSV import jobs.
"""

import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.common.managers import WorkspaceScopedManager
from apps.common.validators import validate_hex_color


class ContentCategory(models.Model):
    """Content category for posts (e.g., Educational, Promotional, Behind the scenes).

    Categories are defined per workspace and used for calendar filtering,
    analytics filtering, and queue-based scheduling.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="content_categories",
    )
    name = models.CharField(max_length=100)
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        help_text="Hex color for calendar display, e.g. #FF5733",
        validators=[validate_hex_color],
    )
    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_content_category"
        ordering = ["position", "name"]
        unique_together = [("workspace", "name")]
        verbose_name_plural = "content categories"

    def __str__(self):
        return self.name


class Tag(models.Model):
    """A reusable tag scoped to a workspace.

    Tags can be applied to Posts, Ideas, and other content types.
    They are workspace-scoped so each workspace has its own tag namespace.

    A tag always files the post internally. Its kind decides whether it also
    reaches the audience: INTERNAL stays behind, HASHTAG and MENTION are
    appended to the caption as "#name" and "@name". Keeping the two jobs in
    one tag is the point — "natal2026" should group the campaign in the
    calendar and tag the post, without being typed twice.
    """

    class Kind(models.TextChoices):
        #: Filing only — never reaches the published post.
        INTERNAL = "internal", _("Internal only")
        #: Published as #name.
        HASHTAG = "hashtag", _("Hashtag")
        #: Published as @name.
        MENTION = "mention", _("Mention")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=100)
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.INTERNAL,
        help_text="Whether this tag is also published, and in what form.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_tag"
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name

    #: Sigil each published kind carries. Absent means the tag stays internal.
    PREFIXES = {Kind.HASHTAG: "#", Kind.MENTION: "@"}

    @property
    def rendered(self):
        """How the tag reads in a caption, or "" when it is internal only."""
        prefix = self.PREFIXES.get(self.kind)
        return f"{prefix}{self.slug_for_publishing(self.name)}" if prefix else ""

    @staticmethod
    def slug_for_publishing(name):
        """A tag name as the networks accept it after the sigil.

        They break a hashtag at the first space or punctuation, so "Black
        Friday" would publish as "#Black" and lose the rest. Folding to
        alphanumerics keeps the whole phrase in one tag. Accented letters
        stay: the networks handle them, and dropping them would mangle
        Portuguese words.
        """
        return "".join(ch for ch in (name or "") if ch.isalnum())

    @classmethod
    def publishable_suffix(cls, workspace_id, tag_names):
        """The "#one @two" trailer for a post's tags, in the order they were applied.

        Unknown names are skipped rather than guessed at: a tag the workspace
        does not define has no kind, and publishing it as a hashtag would put
        a word in the post that nobody chose to publish.
        """
        if not tag_names:
            return ""
        by_name = {t.name: t for t in cls.objects.for_workspace(workspace_id).filter(name__in=tag_names)}
        rendered = []
        for name in tag_names:
            tag = by_name.get(name)
            if tag and (text := tag.rendered) and text not in rendered:
                rendered.append(text)
        return " ".join(rendered)


class TagGroup(models.Model):
    """A named set of a brand's tags, of any kind.

    The tag pickers list the tags under their groups and apply a whole group
    at once. A tag may sit in several groups ("culture" under two dates) or
    in none. A group is made by hand, or when an idea started from a special
    date is saved: the date's name becomes the group, its tags the content.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="tag_groups",
    )
    name = models.CharField(max_length=100)
    tags = models.ManyToManyField(Tag, related_name="groups", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_tag_group"
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name


class IdeaGroup(models.Model):
    """A Kanban column/group for organising ideas, scoped to a workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="idea_groups",
    )
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea_group"
        ordering = ["position", "created_at"]

    def __str__(self):
        return self.name


class Idea(models.Model):
    """A content idea on the Kanban board, scoped to a workspace."""

    class Status(models.TextChoices):
        UNASSIGNED = "unassigned", _("Unassigned")
        TODO = "todo", _("To Do")
        IN_PROGRESS = "in_progress", _("In Progress")
        DONE = "done", _("Done")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="ideas",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_ideas",
    )

    # Content
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idea_usages",
    )

    # Kanban
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNASSIGNED,
        db_index=True,
    )
    group = models.ForeignKey(
        IdeaGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ideas",
    )
    position = models.PositiveIntegerField(default=0)

    # The day the content is for (a special date, a launch). Art requests made
    # from the idea are due a few days before it.
    # The icon the brand picked for the card: a slug of the library in
    # ``idea_icons.py``; empty shows the default one. db_default keeps inserts
    # from code that predates the column working during a rolling deploy.
    icon = models.CharField(max_length=32, blank=True, default="", db_default="")
    # The colour picked for the icon (``idea_icons.TONES``); empty takes the
    # colour of the column the card is in.
    icon_tone = models.CharField(max_length=16, blank=True, default="", db_default="")
    target_date = models.DateField(null=True, blank=True)
    calendar_event = models.ForeignKey(
        "calendar.CustomCalendarEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ideas",
    )

    # Optional link to a Post (when idea is converted)
    post = models.OneToOneField(
        "Post",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_idea",
    )

    # Timestamps
    #: The brand's accounts the idea is meant for; "create post" starts the
    #: post on them (on every connected account when none is ticked).
    channels = models.ManyToManyField("social_accounts.SocialAccount", blank=True, related_name="ideas")
    #: The formats each channel is meant for, as the composer's toggles name
    #: them (``{"<account id>": ["default", "reel"]}``); ``channels`` holds
    #: the accounts that have any.
    channel_formats = models.JSONField(
        default=dict, blank=True, db_default=models.Value({}, output_field=models.JSONField())
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea"
        ordering = ["position", "-created_at"]

    def __str__(self):
        return f"Idea({self.group or self.status}): {self.title[:50]}"


class IdeaMedia(models.Model):
    """Media attachment on an idea, with stable ordering."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idea = models.ForeignKey(
        Idea,
        on_delete=models.CASCADE,
        related_name="media_attachments",
    )
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.CASCADE,
        related_name="idea_attachments",
    )
    position = models.PositiveIntegerField(default=0, help_text="Ordering position on the idea.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_idea_media"
        ordering = ["position", "created_at"]
        unique_together = [("idea", "media_asset")]

    def __str__(self):
        return f"IdeaMedia(pos={self.position}): {self.media_asset.filename}"


class Post(models.Model):
    """A piece of content created in the composer.

    A Post holds the shared/base content. PlatformPost children hold
    per-platform overrides **and their own editorial status** — each social
    account flows through the workflow independently so one platform can be
    a draft while another is scheduled or already published.

    ``Post.status`` is a derived aggregate over ``platform_posts`` (see
    ``apps.composer.status.derive_post_status``), kept for list/dashboard
    rendering and backwards compatibility with existing templates.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="posts",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_posts",
    )

    # Content
    title = models.CharField(max_length=255, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    first_comment = models.TextField(blank=True, default="")
    internal_notes = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    category = models.ForeignKey(
        ContentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posts",
    )

    # Scheduling (default when a PlatformPost doesn't set its own scheduled_at)
    scheduled_at = models.DateTimeField(blank=True, null=True, db_index=True)
    published_at = models.DateTimeField(blank=True, null=True)

    # Optional draft-stage suggestion of when to publish — distinct from the
    # committed ``scheduled_at``. Lets a planner/agent record "this should go
    # out Tue 9am" without queuing it for the publisher. Cleared once the post
    # is actually scheduled.
    proposed_publish_at = models.DateTimeField(blank=True, null=True)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_post"
        ordering = ["-created_at"]

    def __str__(self):
        snippet = (self.caption[:50] + "...") if len(self.caption) > 50 else self.caption
        return f"Post({self.status}): {snippet or '(no caption)'}"

    # ------------------------------------------------------------------
    # Derived status (aggregated across PlatformPost children)
    # ------------------------------------------------------------------

    @property
    def status(self):
        from .status import derive_post_status

        # Use prefetch-friendly iteration so list/grid views don't trigger an
        # extra query per post when ``platform_posts`` is already prefetched.
        statuses = [pp.status for pp in self.platform_posts.all()]
        return derive_post_status(statuses)

    def get_status_display(self):
        """Human label mirroring the old Django-generated method."""
        return dict(PlatformPost.Status.choices).get(self.status, self.status)

    @property
    def status_color(self):
        return PlatformPost.STATUS_COLORS.get(self.status, "gray")

    @property
    def is_editable(self):
        """Whether the post can be edited in the composer."""
        return self.status in (
            "draft",
            "changes_requested",
            "rejected",
            "approved",
            "scheduled",
        )

    @property
    def is_schedulable(self):
        """Whether the post can be scheduled."""
        return self.status in ("draft", "approved")

    @property
    def caption_snippet(self):
        """First 100 characters of caption for preview."""
        if len(self.caption) <= 100:
            return self.caption
        return self.caption[:100] + "…"

    @property
    def platform_posts_summary(self):
        """Summary of target platforms."""
        return list(self.platform_posts.values_list("social_account__platform", flat=True))


class PlatformPost(models.Model):
    youtube_video_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    """A per-platform variant of a Post.

    Created for each selected social account when composing. Holds optional
    caption/media overrides and — crucially — owns its own editorial status,
    so each social account flows through the workflow independently.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING_REVIEW = "pending_review", _("Pending Review")
        PENDING_CLIENT = "pending_client", _("Pending Client")
        APPROVED = "approved", _("Approved")
        CHANGES_REQUESTED = "changes_requested", _("Changes Requested")
        REJECTED = "rejected", _("Rejected")
        SCHEDULED = "scheduled", _("Scheduled")
        PUBLISHING = "publishing", _("Publishing")
        PUBLISHED = "published", _("Published")
        FAILED = "failed", _("Failed")
        ON_HOLD = "on_hold", _("On Hold")

    # Every field :meth:`transition_to` may write. A caller that passes
    # ``update_fields`` MUST include these or the transition half-applies
    # silently — which is exactly how the retry-budget and publish-handle
    # resets went missing on three of the five scheduling paths. Spell the
    # set once here rather than re-listing it at each call site.
    TRANSITION_FIELDS = (
        "status",
        "published_at",
        "platform_post_id",
        "retry_count",
        "next_retry_at",
        "publish_error",
    )

    # Statuses that must never be removed by *accidental* deletion paths
    # (composer account deselection, autosave sync): a published or
    # mid-publish row is history, and deleting it cascades away its
    # PublishLog records. Explicit deletion (the post delete action) is the
    # user's call and intentionally bypasses this.
    PROTECTED_STATUSES = (Status.PUBLISHED, Status.PUBLISHING)

    # Statuses whose scheduled time may be changed by a calendar drag-and-drop.
    # A dropped draft/failed chip becomes ``scheduled`` (an implicit
    # (re)schedule / retry); the others just move in time, keeping their status.
    # ``published`` and ``publishing`` are excluded — the post is done or in
    # flight and must not be dragged. Shared by the calendar chip templates and
    # the reschedule endpoint so both agree on what's draggable.
    RESCHEDULABLE_STATUSES = (
        Status.DRAFT,
        Status.APPROVED,
        Status.SCHEDULED,
        Status.FAILED,
        Status.PENDING_REVIEW,
        Status.PENDING_CLIENT,
    )

    # Valid state transitions (from → set of allowed targets). Mirrors the old
    # Post-level state machine minus ``partially_published`` — that concept
    # only applies at the aggregate/Post level and is produced by
    # ``derive_post_status``.
    VALID_TRANSITIONS = {
        "draft": {"pending_review", "scheduled", "publishing"},
        "pending_review": {"approved", "changes_requested", "rejected"},
        "approved": {"pending_client", "scheduled", "publishing", "draft", "on_hold", "pending_review"},
        "pending_client": {"approved", "changes_requested", "rejected"},
        "changes_requested": {"pending_review", "draft"},
        "rejected": {"draft", "pending_review"},
        "scheduled": {"publishing", "draft"},
        "publishing": {"published", "failed", "scheduled"},  # scheduled = retry
        "failed": {"publishing", "draft", "scheduled"},
        # Client-requested hold: parked out of the publish path. The team resolves
        # it back to approved (resume), draft (rework), or changes_requested. There
        # is deliberately no on_hold → scheduled edge — un-hold to ``approved`` first
        # so nothing publishes straight out of a hold.
        "on_hold": {"approved", "draft", "changes_requested"},
        "published": set(),  # terminal
    }

    class Format(models.TextChoices):
        """What this row publishes on its account.

        One post can go to several formats of the same account at once (a Feed
        post and a Story), and each is its own published object with its own
        status, platform id, retries and metrics — hence one row per format.
        ``DEFAULT`` is whatever the platform publishes normally (a feed post, a
        video, a pin); its exact shape is inferred from the media.
        """

        DEFAULT = "default", _("Post")
        STORY = "story", pgettext_lazy("publishing format", "Story")
        REEL = "reel", _("Reels")
        PROFILE_PHOTO = "profile_photo", _("Photo")

    class FirstCommentStatus(models.TextChoices):
        NONE = "none", _("Not requested")
        PENDING = "pending", _("Pending")
        POSTED = "posted", _("Posted")
        FAILED = "failed", _("Failed")

    STATUS_COLORS = {
        "draft": "gray",
        "pending_review": "orange",
        "pending_client": "amber",
        "approved": "teal",
        "changes_requested": "orange",
        "rejected": "red",
        "scheduled": "blue",
        "publishing": "indigo",
        "published": "green",
        "partially_published": "yellow",  # only used by Post-level aggregate
        "failed": "red",
        "on_hold": "violet",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="platform_posts",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="platform_posts",
    )
    format = models.CharField(
        max_length=20,
        choices=Format.choices,
        default=Format.DEFAULT,
        db_default=Format.DEFAULT,
    )

    # Per-platform overrides (null = use base post values)
    platform_specific_title = models.TextField(blank=True, null=True)
    platform_specific_caption = models.TextField(blank=True, null=True)
    platform_specific_media = models.JSONField(
        blank=True,
        null=True,
        help_text="JSON list of media asset IDs with platform-specific ordering/cropping.",
    )
    platform_specific_first_comment = models.TextField(blank=True, null=True)
    platform_extra = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-platform metadata (privacy, tags, thumbnail_asset_id, etc.)",
    )

    # Editorial + publishing state
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    platform_post_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The post ID on the platform after publishing.",
    )
    publish_error = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(blank=True, null=True)

    # First-comment outcome. Kept in dedicated columns rather than in
    # ``platform_extra``: that field is an *input* channel — the publish engine
    # merges it into ``PublishContent.extra``, which providers forward into
    # their API payloads — so status written there would ride along on the next
    # publish. A post whose comment failed must also be distinguishable from a
    # fully successful one, which needs a queryable column.
    first_comment_status = models.CharField(
        max_length=10,
        choices=FirstCommentStatus.choices,
        default=FirstCommentStatus.NONE,
        db_index=True,
    )
    first_comment_id = models.CharField(max_length=255, blank=True, default="")
    first_comment_error = models.TextField(blank=True, default="")
    first_comment_retry_count = models.PositiveIntegerField(default=0)
    first_comment_posted_at = models.DateTimeField(blank=True, null=True)
    scheduled_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Per-platform scheduled publish time. NULL falls back to Post.scheduled_at.",
    )
    retry_count = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(blank=True, null=True)

    # Analytics sync state — the read-side counterpart to the retry budget
    # above, owned solely by ``apps.analytics.tasks``.
    #
    # ``analytics_attempted_at`` is the last time the sync *tried* this post's
    # per-post metrics fetch, successful or not. The cadence ladder used to be
    # derived from the newest ``PostInsightsSnapshot.captured_at`` instead,
    # which meant a post whose fetch kept failing wrote no row, looked
    # never-synced, and was retried on every hourly tick forever. That is the
    # amplification that emptied a whole day of YouTube API quota in an hour.
    #
    # NULL means "never attempted under this scheme"; the sync falls back to the
    # old snapshot signal for those, so no backfill is needed.
    analytics_attempted_at = models.DateTimeField(blank=True, null=True)

    # Consecutive failed attempts, reset to 0 by any success. Drives the
    # exponential backoff in ``apps.analytics.tasks._analytics_failure_backoff``
    # so a permanently unfetchable post (deleted video, revoked visibility)
    # costs one call a week rather than one an hour.
    analytics_failure_count = models.PositiveSmallIntegerField(default=0, db_default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_platform_post"
        constraints = [
            models.UniqueConstraint(
                fields=["post", "social_account", "format"],
                name="uniq_pp_post_account_format",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="idx_pp_status_sched",
            ),
        ]

    def __str__(self):
        return f"PlatformPost({self.social_account.platform}/{self.format}): {self.status}"

    @property
    def is_story(self):
        return self.format == self.Format.STORY

    @property
    def network_delete(self) -> str:
        """What deleting this row can do about the post on the network.

        "" when nothing of it is live, "yes" when the network deletes it
        through our connection, "no" when it stays live whatever we do.
        """
        if self.status != self.Status.PUBLISHED:
            return ""
        if self.platform_post_id and self.social_account.can_delete_post(self.format):
            return "yes"
        return "no"

    # Recommended aspect ratio of the art per format, and per platform for the
    # ordinary post. Story, Reels, profile photo and the Instagram feed match the
    # composer's crop rules (compose.html ratioRule); the rest are the platforms'
    # own recommendations. Shown next to the format, e.g. in the calendar's post card.
    FORMAT_RATIOS = {"story": "9:16", "reel": "9:16", "profile_photo": "1:1"}
    FEED_RATIOS = {
        "instagram": "4:5 – 1.91:1",
        "instagram_login": "4:5 – 1.91:1",
        "facebook": "1:1 / 4:5",
        "threads": "4:5",
        "linkedin": "1.91:1 / 1:1",
        "linkedin_personal": "1.91:1 / 1:1",
        "linkedin_company": "1.91:1 / 1:1",
        "tiktok": "9:16",
        "youtube": "16:9",
        "pinterest": "2:3",
        "x": "16:9",
        "bluesky": "16:9",
        "mastodon": "16:9",
        "google_business": "4:3",
    }

    @property
    def recommended_ratio(self):
        """The art's recommended aspect ratio for this format and platform, or ""."""
        if self.format in self.FORMAT_RATIOS:
            return self.FORMAT_RATIOS[self.format]
        return self.FEED_RATIOS.get(self.social_account.platform, "")

    @property
    def format_label(self):
        """The format as the user reads it, or "" for the platform's ordinary post."""
        if self.format == self.Format.DEFAULT:
            return ""
        return self.get_format_display()

    @property
    def delivered_by_notification(self):
        """Settled by notifying someone to post it by hand, not by publishing it.

        See apps/publisher/manual_delivery: the row is ``published`` because the
        system's part is done, but nothing reached the platform, so the UI says
        "Notified" instead.
        """
        return self.status == self.Status.PUBLISHED and (self.platform_extra or {}).get("delivery") == "notify"

    @property
    def is_reschedulable(self):
        """Whether this chip may be dragged to a new date on the calendar."""
        return self.status in self.RESCHEDULABLE_STATUSES

    @property
    def is_bulk_selectable(self):
        """Whether this row can be the target of a bulk draft/publish/delete.

        Every branch of ``bulk_platform_action`` skips ``PROTECTED_STATUSES``, so
        offering a checkbox on those rows would only ever be a no-op.
        """
        return self.status not in self.PROTECTED_STATUSES

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def can_transition_to(self, new_status):
        """Check if a status transition is valid."""
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        return new_status in allowed

    def transition_to(self, new_status):
        """Transition to a new status, raising ValueError if invalid."""
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Invalid status transition: {self.status} → {new_status}. "
                f"Allowed: {self.VALID_TRANSITIONS.get(self.status, set())}"
            )
        self.status = new_status
        if new_status == "published":
            self.published_at = timezone.now()
        if new_status == "scheduled":
            # A deliberate re-schedule is a fresh start, so the old attempt's
            # backoff must not carry over: a spent retry_count would otherwise
            # fail the post on its first attempt instead of giving it the full
            # budget, and a stale next_retry_at would hold it back from the due
            # query. Safe to do here because the publish engine's own
            # retry path sets ``status`` directly and never comes through this
            # method — so this cannot clear a backoff it just scheduled.
            self.retry_count = 0
            self.next_retry_at = None
            self.publish_error = ""
            # The handle belongs to the *previous* attempt. Left in place, the
            # confirmation sweep would treat it as this attempt's and settle the
            # post against an outcome that has nothing to do with it — reporting
            # a publish that never reached the platform as done.
            self.platform_post_id = ""

    @property
    def status_color(self):
        return self.STATUS_COLORS.get(self.status, "gray")

    @property
    def is_editable(self):
        return self.status in (
            "draft",
            "changes_requested",
            "rejected",
            "approved",
            "scheduled",
        )

    @property
    def is_schedulable(self):
        return self.status in ("draft", "approved")

    @property
    def effective_title(self):
        """Return platform-specific title or fall back to base post title."""
        if self.platform_specific_title is not None:
            return self.platform_specific_title
        return self.post.title

    @property
    def effective_caption(self):
        """The caption as published: the chosen text plus any published tags.

        The trailer is appended here rather than at publish time so that
        everything downstream — the character count, the limit check, the
        preview — is measuring the text that actually goes out.
        """
        base = self.platform_specific_caption if self.platform_specific_caption is not None else self.post.caption
        suffix = Tag.publishable_suffix(self.post.workspace_id, self.post.tags)
        if not suffix:
            return base
        return f"{base}\n\n{suffix}" if base.strip() else suffix

    @property
    def effective_first_comment(self):
        """Return platform-specific first comment or fall back to base."""
        if self.platform_specific_first_comment is not None:
            return self.platform_specific_first_comment
        return self.post.first_comment

    @property
    def platform(self):
        return self.social_account.platform

    @property
    def char_limit(self):
        return self.social_account.char_limit

    @property
    def caption_length(self):
        # Not len(): platforms that escape reserved characters send more than
        # was typed, and the limit applies to what is sent.
        return self.social_account.caption_wire_length(self.effective_caption)

    @property
    def is_over_limit(self):
        return self.caption_length > self.char_limit


# Backwards-compat alias: lots of existing code imports ``Post.Status.DRAFT`` /
# iterates ``Post.Status.choices`` to build filter UIs. The enum lives on
# PlatformPost now; re-expose it on Post so callers don't need updating.
Post.Status = PlatformPost.Status  # type: ignore[attr-defined]


class PostMedia(models.Model):
    """Media attachment on a post, with ordering and alt text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="media_attachments",
    )
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.CASCADE,
        related_name="post_usages",
    )
    position = models.PositiveIntegerField(default=0, help_text="Ordering position in carousel.")
    alt_text = models.TextField(blank=True, default="")
    platform_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text='Per-platform crop/format overrides, e.g. {"instagram": {"crop": "4:5"}}',
    )

    class Meta:
        db_table = "composer_post_media"
        ordering = ["position"]
        unique_together = [("post", "media_asset")]

    def __str__(self):
        return f"PostMedia(pos={self.position}): {self.media_asset.filename}"


class PostVersion(models.Model):
    """Immutable snapshot of a post state for version history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    snapshot = models.JSONField(
        help_text="Full post state at time of save (caption, media, platforms, etc.).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "composer_post_version"
        ordering = ["-version_number"]
        unique_together = [("post", "version_number")]

    def __str__(self):
        return f"PostVersion(v{self.version_number}) for {self.post_id}"


class PostTemplate(models.Model):
    """Reusable post template scoped to a workspace.

    Stores a snapshot of post configuration (caption, media references,
    category, platform selections, first comment, hashtags) that can be
    loaded into the composer as a starting point.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="post_templates",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    template_data = models.JSONField(
        default=dict,
        help_text=("JSON snapshot: caption, first_comment, category_id, platform_ids, hashtags, media_asset_ids, tags"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_post_template"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CSVImportJob(models.Model):
    """Tracks a bulk CSV import job for posts."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="csv_import_jobs",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    file = models.FileField(upload_to="csv_imports/")
    column_mapping = models.JSONField(
        default=dict,
        help_text="Maps CSV column indices to post fields.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    result_summary = models.JSONField(
        default=dict,
        help_text='{"created": N, "errors": N, "warnings": [...]}',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_csv_import_job"
        ordering = ["-created_at"]

    def __str__(self):
        return f"CSVImportJob({self.status}): {self.total_rows} rows"


class Feed(models.Model):
    """An RSS feed subscription scoped to a workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="feeds",
    )
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=500, help_text="RSS feed URL")
    website_url = models.URLField(max_length=500, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_feed"
        ordering = ["name"]
        unique_together = [("workspace", "url")]

    @property
    def favicon_url(self):
        """Return a best-effort favicon URL derived from website_url."""
        if not self.website_url:
            return ""

        parsed = urlsplit(self.website_url)
        if not parsed.scheme or not parsed.netloc:
            return ""

        return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"

    def __str__(self):
        return self.name


class IdeaTemplate(models.Model):
    """A ready-made starting point shown in the create page's Templates tab.

    The same for every brand, and kept by the operator in the administrative
    area. The integer id is what the composer receives as ``?template=<id>``;
    the rows that shipped with the product keep the ids they had in
    ``builtin_templates.py``, from which the table was first filled.
    """

    class Category(models.TextChoices):
        TIP = "tip", _("Tip")
        CASE_STUDY = "case-study", _("Case Study")
        STORY = "story", _("Story")
        HOW_TO = "how-to", _("How-to")
        QUESTION = "question", _("Question")
        OPINION = "opinion", _("Opinion")
        LIST = "list", _("List")
        BEHIND_THE_SCENES = "behind-the-scenes", _("Behind the Scenes")

    #: Where the template's content fits, shown as "Format" on its card.
    class Format(models.TextChoices):
        POST = "post", _("Post")
        CAROUSEL = "carousel", _("Carousel")
        REELS = "reels", _("Reels")
        STORY = "story", _("Stories")
        VIDEO = "video", _("Video")

    #: What the template is for, shown as "Goal".
    class Goal(models.TextChoices):
        ENGAGEMENT = "engagement", _("Engagement")
        AUTHORITY = "authority", _("Authority")
        SALES = "sales", _("Sales")
        REACH = "reach", _("Reach")
        COMMUNITY = "community", _("Community")
        EDUCATION = "education", _("Education")

    #: The networks it suits (brand names, not translated).
    class Network(models.TextChoices):
        INSTAGRAM = "instagram", "Instagram"
        FACEBOOK = "facebook", "Facebook"
        LINKEDIN = "linkedin", "LinkedIn"
        TIKTOK = "tiktok", "TikTok"
        YOUTUBE = "youtube", "YouTube"
        THREADS = "threads", "Threads"
        PINTEREST = "pinterest", "Pinterest"
        X = "x", "X"

    id = models.AutoField(primary_key=True)
    emoji = models.CharField(max_length=8, blank=True, default="")
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=400, blank=True, default="")
    body = models.TextField(blank=True, default="")
    category = models.CharField(max_length=24, choices=Category.choices, default=Category.TIP)
    is_active = models.BooleanField(default=True)
    #: The template's icon from the idea icon library (``idea_icons``);
    #: empty means its category's. ``emoji`` is no longer shown.
    icon = models.CharField(max_length=32, blank=True, default="", db_default="")
    #: The card's cover; without one the card shows the category's icon.
    image = models.ImageField(upload_to="idea_templates/", blank=True, default="", db_default="")
    #: The longer text of the template's detail panel.
    details = models.TextField(blank=True, default="", db_default="")
    formats = models.JSONField(default=list, blank=True, db_default=models.Value([], output_field=models.JSONField()))
    goals = models.JSONField(default=list, blank=True, db_default=models.Value([], output_field=models.JSONField()))
    networks = models.JSONField(default=list, blank=True, db_default=models.Value([], output_field=models.JSONField()))
    #: The publishing formats per network, as the composer offers them
    #: (``{"instagram": ["default", "reel"]}``; formats from
    #: ``SocialAccount.PLATFORM_FIELD_CONFIG``). ``networks`` holds its keys.
    network_formats = models.JSONField(
        default=dict, blank=True, db_default=models.Value({}, output_field=models.JSONField())
    )
    ideal_for = models.CharField(max_length=200, blank=True, default="", db_default="")

    #: Where a template stands: the operator's own are approved; one a
    #: customer sent waits hidden (``is_active`` off) until the operator
    #: approves it, which also puts it on the shelf.
    class Review(models.TextChoices):
        APPROVED = "approved", _("Approved")
        PENDING = "pending", _("Waiting for approval")
        REJECTED = "rejected", _("Rejected")

    review_status = models.CharField(
        max_length=16, choices=Review.choices, default=Review.APPROVED, db_default=Review.APPROVED
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    submitted_workspace = models.ForeignKey(
        "workspaces.Workspace", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_idea_template"
        ordering = ["-id"]

    def __str__(self):
        return self.title


class IdeaTemplateVariation(models.Model):
    """Another version of a template's text, kept by the operator.

    The template's own ``body`` is its first version; these follow it in
    ``position`` order, and the customer picks which one to open.
    """

    id = models.AutoField(primary_key=True)
    template = models.ForeignKey(IdeaTemplate, on_delete=models.CASCADE, related_name="variations")
    title = models.CharField(max_length=120, blank=True, default="")
    body = models.TextField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "composer_idea_template_variation"
        ordering = ["position", "id"]

    def __str__(self):
        return self.title or self.template.title


class IdeaTemplateFavorite(models.Model):
    """A template a brand saved to find again."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="favorite_idea_templates"
    )
    template = models.ForeignKey(IdeaTemplate, on_delete=models.CASCADE, related_name="favorites")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea_template_favorite"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "template"], name="uniq_workspace_idea_template_favorite"),
        ]


class IdeaTemplateUse(models.Model):
    """One use of a template by a brand: opened in the composer or put on the
    idea board. The most used ones lately are shown as popular."""

    id = models.BigAutoField(primary_key=True)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="idea_template_uses")
    template = models.ForeignKey(IdeaTemplate, on_delete=models.CASCADE, related_name="uses")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea_template_use"
