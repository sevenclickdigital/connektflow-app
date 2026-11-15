# Feature Reference

What the product does today, and where each feature lives in the code. It replaces the original planning spec (`development_specs/feature-spec-social-media-management-v2.md`, now only in git history). Section numbers (F-x.y) follow the spec where a feature came from it. Features added later have no number.

Conventions used below:

- Everything is scoped Organization → Workspace. Most models carry a `workspace` FK and use `WorkspaceScopedManager` (`apps/common/managers.py`).
- Workspace URLs are mounted under `/workspace/<workspace_id>/...` (see `config/urls.py`).
- Background work runs in the `process_tasks` worker (django-background-tasks; `apps/common/background.py`, `apps/common/worker.py`). There is no Celery or Redis requirement.
- Defaults referenced as `publishing.retry_max_attempts` etc. come from `apps/settings_manager/defaults.py` (`APP_DEFAULTS`), and can be overridden per org/workspace via `OrgSetting` / `WorkspaceSetting` rows. Those rows are edited in Django admin. The `/settings/` page is a static page with no editor.
- UI languages are Português (Brasil) and English (US) (`LANGUAGES` in `config/settings/base.py`). The language picker posts to `/i18n/`. Translation rules are in `.claude/skills/translation/SKILL.md`.

## Supported platforms

From `README.md`. Provider implementations are in `providers/` (one module per platform, all implementing `providers/base.py`).

| Platform | Publish | Comments | DMs | Insights |
|---|:-:|:-:|:-:|:-:|
| Facebook Pages | ✓ | ✓ | ✓ | ✓ |
| Instagram (via Facebook Login) | ✓ | ✓ | ✓ | ✓ |
| Instagram (Direct / Instagram Login) | ✓ | ✓ | ✓ | ✓ |
| LinkedIn Personal / Company | ✓ | ✓ | — | ✓ |
| TikTok | ✓ | — | — | ✓ |
| YouTube | ✓ | ✓ | — | ✓ |
| Pinterest | ✓ | — | — | ✓ |
| X | ✓ | — | — | ✓ |
| Threads | ✓ | ✓ | — | ✓ |
| Bluesky | ✓ | ✓ | — | — |
| Google Business Profile | ✓ | — | — | ✓ |
| Mastodon | ✓ | ✓ | — | — |
| DEV.to | ✓ | — | — | — |
| WhatsApp (Cloud API) | — | — | ✓ | — |

X publishes text with up to 4 photos or one video/GIF through the v2 API (`providers/x.py`, OAuth 2.0 with PKCE); it needs an X app on a paid tier with write access, configured with `PLATFORM_X_CLIENT_ID` / `PLATFORM_X_CLIENT_SECRET`. Instagram (Direct) still works but is hidden from the connect page by default, because new Meta apps no longer expose the Instagram App ID it needs. It is hidden through `PlatformVisibility` (`apps/social_accounts/models.py`) and can be turned back on in admin.

---

## 1. Organization, workspaces, members

### F-1.0 Account profile — `apps/accounts/`
- Account centre → Profile (`accounts:settings`): photo, first and last name, postal address (CEP, street, number, complement, neighbourhood, city, UF) and password. `ProfileForm` (`apps/accounts/forms.py`) writes `User.name` from first + last name, normalises the CEP to `00000-000` and accepts the address only complete (complement is the one optional part) or empty. Accounts that predate first/last name are offered their `name` split. The data goes with the user when the account is deleted. It is not sent to Asaas yet.

### F-1.1 Organizations — `apps/organizations/`
- `Organization`: name, logo URL, default timezone, billing email.
- Settings page `/organizations/settings/`. The workspace list is at `/organizations/workspaces/`.
- Deletion: the owner requests it. That sets `deletion_requested_at` and schedules `execute_scheduled_org_deletion` (`apps/organizations/tasks.py`) for **14 days** later, and the deletion can be cancelled during that window. YouTube connections and copied YouTube API data are removed immediately when deletion is requested (`apps/social_accounts/youtube_compliance.py`). After deletion the owner is redirected to `CUSTOM_SIGNUP_URL` if it is set.

### F-1.2 Workspaces — `apps/workspaces/`
- `Workspace`: name, icon, description, timezone, primary/secondary brand colour, default hashtags, default first comment, approval workflow mode, `is_archived`.
- Create, list and settings pages, plus a separate approvals settings page (`/workspaces/<id>/settings/approvals/`).
- Cross-workspace calendar at `/organizations/calendar/` (`apps/organizations/views.py`).
- A workspace can connect at most `MAX_ACCOUNTS_PER_PLATFORM` (default **1**) account per platform (`apps/social_accounts/views.py`). Reconnecting an account that already exists is always allowed.

### F-1.3 Members & RBAC — `apps/members/`
- Org roles: Owner, Admin, Member (`OrgMembership`). Org-level permissions are `manage_intelligence_billing`, `use_intelligence` and `manage_api_keys`.
- Workspace roles: Owner, Manager, Editor, Contributor, Client, Viewer (`WorkspaceMembership`). The permission keys in `PERMISSION_KEYS` are create/edit-others/approve/publish-directly posts, manage social accounts, view analytics, use inbox, reply from inbox, manage workspace settings, and upload/edit/delete/manage media. The role-to-permission map is `BUILTIN_ROLE_PERMISSIONS`.
- `CustomRole` exists and is honoured by permission checks (`effective_permissions`), but custom roles can only be created and assigned in Django admin.
- Invitations (`Invitation`): invite, resend, revoke, accept-by-token. Invites expire after 7 days (`org.invitation_expiry_days`) and are single-use even if two signups use them at the same time. Per-member workspace assignment is at `members/<id>/workspaces/`.
- Enforcement: `apps/members/decorators.py` (`require_permission`, `require_org_permission`) and `apps/members/middleware.py`.

### F-1.4 Client portal — `apps/client_portal/`
- Clients are `WorkspaceMembership` rows with the Client role. They are invited from workspace settings → Clients (`urls_admin.py`).
- Access is passwordless: a magic link (`MagicLinkToken`) is valid for 30 days, and the expired-link page lets the client ask for a new one.
- Portal pages under `/portal/`: dashboard, approval queue (approve / request changes / reject / **request hold**), published posts, activity. `/portal/reports/` is a placeholder page.

### F-1.5 Platform API credentials — `apps/credentials/`
- `PlatformCredential` stores encrypted per-org app credentials for each platform and is edited in Django admin.
- `resolve_platform_credentials()` uses `.env` values (`PLATFORM_*`) when they cover all the keys a platform requires, and falls back to the org's admin-entered row otherwise. The connect page only offers platforms that have credentials configured.

### F-1.6 Configurable defaults — `apps/settings_manager/`
- The three-level lookup (app default → org → workspace) is in `helpers.py`. The keys in use include approval reminder hours, first-comment delay (120 s), retry attempts/backoff (3, at 1/5/30 min), inbox SLA target, notification quiet hours/digest, and client connection-link expiry.

### F-1.7 Client onboarding — `apps/onboarding/`
- **Connection links** (`ConnectionLink`): a manager creates a tokenised link with an expiry of 1 to 90 days (default 7), sends it by email, and can revoke it. The client opens a public page with no login, connects accounts through OAuth (or Bluesky/Mastodon forms), and the accounts land in the workspace. Each connection is recorded in `ConnectionLinkUsage` and triggers a `client_connected_accounts` notification.
- **Get Started checklist** on the workspace dashboard (`checklist.py`): connect accounts, create first post, create first idea, invite team. It can be dismissed per user (`OnboardingChecklist`).

---

## 2. Content creation & publishing

### F-2.1 Composer — `apps/composer/`
- Model: `Post` holds the shared content (title, caption, first comment, internal notes, tags, category, schedule). It has one `PlatformPost` per target **account and format** (`PlatformPost.format`: `default`, `story`, `reel`, `profile_photo`; unique on post + account + format), so one post can go out as an Instagram feed post and an Instagram Story at once, each with its own status, platform id, retries and metrics. A row can override title, caption, media and first comment and carries platform-specific options in `platform_extra`. **Status lives on `PlatformPost`.** The `Post` status is derived from its children (`apps/composer/status.py`).
- `PlatformPost.Status`: draft, pending_review, pending_client, approved, changes_requested, rejected, scheduled, publishing, published, failed, on_hold.
- Post types (`providers/types.py`): text, image, video, carousel, story, reel, link, article, poll, pin, short, profile_photo. A `story`, `reel` or `profile_photo` row publishes as that type (`FORMAT_POST_TYPES`); a `default` row's type is resolved from the media actually attached.
- Where to publish: the composer shows one card per connected account, with a toggle per format (`SocialAccount.format_options`, formats declared in `PLATFORM_FIELD_CONFIG["formats"]`): Feed / Story / Reels on Facebook and Instagram, post / photo on Google Business, and the ordinary post for every other platform. A count of the chosen destinations and "Clear" sit above the row. The channels on offer but not connected yet (`PlatformVisibility.unconnected_choices`, chat-only platforms excluded) become a single dashed "Connect channel" card. Once media is attached, a format it can't satisfy is greyed out with the reason. The cards are drawn by `templates/partials/_target_card.html`, shared with the art request's network picker (there the whole card is the network tab). The form posts `selected_targets` (`"<account>:<format>,..."`) next to `selected_accounts`. Formats can be picked before any media is attached; the composer warns when the media doesn't fit, and scheduling is refused (`format_blocker`, also checked by `publish_blocker` in the publisher): a Story needs at least one photo or video, a Reel one video, a profile photo one image. Instagram Feed + Reels on a single video is refused because both would publish the same Reel.
- Customize: one "Customize <channel>" chip per chosen format ("Customize Facebook Stories"), labelled by channel since a workspace has one account per platform. Each carries that format's own caption/title (`override_caption_<account>_<format>`, read by `_target_field`) and its options: the notify toggle for a Story, the gallery category for a Google Business photo, place and feeling for Facebook (account-wide, in the card of the first of feed/Reels chosen; each field is on screen only where the live API takes it — `providers/meta_feed_extras.py` has the table: place and feeling on a feed post without video, place alone on a Reel, sent on the finish phase and dropped on a refusal, neither on a feed video or a Story), and collaborators (up to 3), tagged people and location for Instagram (in the card of the account's first chosen format; account-wide, stored on every Instagram row; `providers/meta_feed_extras.apply_instagram_extras` sends each only where Instagram takes it: collaborators and location on a Feed image, the carousel parent and Reels, tags on the photo, the first photo of a carousel, Reels and Stories, with a position on photos only); while Stories is the only Instagram format chosen, the card offers the tags alone. With Reels chosen, the same card offers the Reel's cover as a moment of the video (the frame picker shared with TikTok, `composer/partials/_video_cover_picker.html`, sent as `thumb_offset`) and whether the Reel also shows on the profile grid (`share_to_feed`). The place picker (`composer/partials/_place_picker.html`, `compose/facebook-places/<account>/`) searches Meta's location Pages; Meta only answers it once the app has the Page Public Metadata Access feature, and until then the picker shows that reason. A chip turns amber when the attached media can't go out in that format.
- A Facebook feed post takes up to 15 photos (`FACEBOOK_MAX_ATTACHED_MEDIA`, exposed as `SocialProvider.max_images_per_post`). The limit is ours: Meta's `attached_media` took 50 when measured, and there is no album to overflow into (`POST /{page-id}/albums` answers `(#3) Application does not have the capability`). `composer.services.image_count_blocker` stops a larger post before it is scheduled — in the composer save, in `create_post` (API/MCP), in `require_publishable` and again in the publisher. Stories are not limited: several photos publish one by one.
- Stories are published without caption or first comment and get no comment automation; Facebook Stories go through `/photo_stories` and `/video_stories`. Meta takes one photo or video per Story, so a Story row with several media goes out as one Story per item, in order (`PublishEngine._publish_story_sequence`). Each live Story's id is saved on the row (`platform_extra["story_progress"]`, keyed to the row so a clone starts over) as soon as it is out; a retry resumes after the last one, and a failure part-way tells the user how many already went out.
- Notified Stories: "Notify me to publish the Story by hand" stores `platform_extra["delivery"] = "notify"` on the Story row. At the scheduled time the publisher does not call the platform (`apps/publisher/manual_delivery.py`): it notifies the author (or, without one, the members who can create posts) in-app and by e-mail with a link to `compose/<post>/manual/<platform_post>/`, a mobile page with the media to download, the caption to copy and an "I've published it" button. The row is settled as `published` with `notified_at` and is shown as "Notified"; it has no platform id, so no metrics, first comment or automation.
- Google Business photo: the `profile_photo` row adds the image to the profile gallery (v4 `media.create`, category stored as `platform_extra["photo_category"]`) instead of creating a local post. The v4 endpoints only route `accounts/{a}/locations/{l}`, so the provider resolves the owning account from the stored `locations/{l}`.
- Formats of one account share its queue slot (`apps/calendar/services._platform_post_for` / `_write_slot`); the slot is freed only when none of them is still scheduled (`drop_queue_entry_unless_scheduled`). Lists and calendar chips tag non-default rows with `templates/partials/_format_badge.html`; deleting from the list removes one format (`post_delete?platform_post=`).
- **Schedule**: the composer's "Schedule" button opens the date and time straight away: an inline calendar (flatpickr, in the page's language) prefilled with the calendar day the post was started on (`?scheduled_date=`; the time defaults to noon, `composer.views.DEFAULT_POST_TIME`), and a 24-hour time field that takes typing (14:30, 1430, 9) or a pick from a list of quarter hours. "Set Schedule" posts `action=schedule`, which the server refuses without a future date and time. The queue actions (`add_to_queue`, `add_to_queue_priority`) are no longer offered in the composer; the endpoint still accepts them.
- Live per-platform preview (`compose/preview/`): one tab per account (the arrows in the panel header step through them) showing that account's card, and a carousel with one slide per chosen format (format tabs above the card, dots below it) (Feed / Story / Reels, Google post / photo), each following that format's caption override. Ordinary posts render through `composer/partials/_preview_post.html` (the Facebook one as a feed grid); Story, Reels and the Google photo through `_preview_vertical.html`.
- Media: the "Add" tile, always last in the row, opens the media library (pick several at once, or upload from it); files dropped on the tile upload straight away. Import from Unsplash (needs `UNSPLASH_ACCESS_KEY`). Each thumbnail has a move handle (drag to reorder) and a settings button whose menu deletes or crops. Video thumbnails come from a picker or an upload.
- Crop (`compose/<post>/crop-media/<media>/`, `compose/crop-pending-media/<asset>/`): an editor with Free, Square, Stories 9:16, Portrait 4:5, Landscape 1.91:1 and 4:3 presets (Cropper.js). Saving makes a cropped copy (`media_library.services.crop_into_new_asset`, counted against the storage quota) and swaps it into the post; the library keeps the original. Story, Reels and Instagram feed customize cards warn about images outside the ratio the format needs; their "Adjust" crops for that format only (`compose/<post>/crop-format-media/`): the copy is listed in that row's `platform_specific_media` and, through `MediaAsset.derived_from`, stands in for its source attachment in that format alone (`composer.services.apply_format_media`, used by the publisher and the preview). The thumbnail menu's Crop still replaces the attachment for every format.
- Platform helpers: Pinterest board list, TikTok creator info (privacy/interaction options), Facebook places and feed extras (`providers/meta_feed_extras.py`).
- First comment: per post or per platform. It is posted after the publish confirms (see F-2.4).
- Autosave, version history (`PostVersion`, with diffs at `approvals/<post_id>/versions/`), clone, delete, drafts list.
- Deleting a published post (composer, the list/board menu, the calendar's post card → `composer:post_delete`): the dialog reads `PlatformPost.network_delete`. Where the network deletes through our connection it offers "Also delete it from <network>" (`remote=1` → `publisher.engine.delete_from_network` → the provider's `delete_post`), ticked by default; where it doesn't, it says the post stays live and only the record goes. A refusal from the network answers 422, shows the reason (`friendly_delete_error`) and keeps the record. What deletes: Facebook feed posts, videos and Reels; Instagram through Facebook Login (posts, Reels, Stories, whole carousels) once `PLATFORM_INSTAGRAM_FACEBOOK_DELETE_SCOPE_ENABLED` is on and the account granted `instagram_manage_contents`. What never does: Facebook Stories, Instagram through Instagram Login, posts run as ads, every other platform. Taking a post down needs the edit right on it (author, or `edit_others_posts`).
- Templates (`PostTemplate`): save-as-template and a template picker, plus built-in templates (`builtin_templates.py`).
- Content categories (`ContentCategory`, with colours) and tags (`Tag` with kind internal / hashtag / mention; a tag can both file the post and be published with it).
- **Idea board**: a Kanban of `Idea` items (unassigned / todo / in progress / done) in user-defined `IdeaGroup` columns, with media. An idea can be turned into a post. An idea may carry a content date (`target_date`) and the calendar event it is for; "Upcoming dates" above the board lists the next special dates and workspace events with a shortcut to start an idea for that day (`templates/composer/partials/_upcoming_dates.html`). "Request arts" in the idea opens the art request (see Tasks).
- **RSS feeds** (`Feed`, `curated_feeds.py`): subscribe to feeds per workspace, with an "explore" catalogue of curated feeds, for content inspiration.
- **CSV import** (`import/csv/`): upload a CSV (max 5 MB), preview it, then confirm to bulk-create posts.
- **Per-post comment automation**: a flow can be attached to a post from the composer (see Automations).

### F-2.2 Approval workflow — `apps/approvals/`
- Workspace mode (`Workspace.ApprovalWorkflowMode`): none, optional, required internal, or required internal + client.
- Actions: submit, approve, request changes, reject, resubmit, resume, and hold (from the client). They apply to the whole post or to a single `PlatformPost`, and every action is logged in `ApprovalAction`. A bulk action endpoint exists.
- Threaded comments (`PostComment`) with internal or external visibility, edit/delete (soft-deleted with `deleted_at`), @mentions (which send a `comment_mention` notification), and attachments served through a permission-checked view.
- Reminders and escalation (`tasks.py`): internal reviewers after 24 h, clients after 48 h, at most 2 reminders per post, then a stalled-approval escalation.

### F-2.3 Calendar & scheduling — `apps/calendar/`
- Month/week/day calendar plus a list mode. Drag-to-reschedule (`reschedule/`) and bulk actions.
- A post starts from a day of the calendar (its "+"; the sidebar's "New → Post" leads there and there is no "Schedule post" menu entry), so a draft sits on its proposed day (`Post.proposed_publish_at`); a draft with no date at all (from an idea, a task, the Radar, or older ones) sits on the day it was created. The composer's Schedule panel opens on today at noon (workspace timezone) for a new post without a day and for a draft without one (`_default_schedule_initial`), so saving it gives it that day. The calendar's "Drafts" button shows or hides drafts on the calendar for the whole workspace (`WorkspaceSetting` key `calendar.show_drafts`, `drafts/toggle/`). Autosave records the panel's day as the draft's proposed day from its first save (so a new draft shows on the day it was started on), and only creates a new post once the composer holds something (text, an account, media, tags or notes).
- Calendar and list share one toolbar card (`templates/calendar/partials/_period_toolbar.html`): month arrows, a month picker (January to December of the year shown), the drafts (calendar only), campaigns and special dates switches, status (calendar) and tag filters. Times follow the workspace timezone; there is no timezone filter. Below it, one filter pill per connected platform (`platform=`, `_platform_pills.html`). The week and day grids still exist (`view=week|day`) but nothing in the page links to them any more.
- Month view shows each day's post count and one compact line per post (platform, time, format icon — Feed, Story or Reels —, first media thumbnail; the title is in the tooltip and the post card via `templates/calendar/partials/_post_thumb.html`; at most `MONTH_DAY_CHIPS` per day). The month fits the screen without scrolling the page: weeks share the height, and `fitDayCells` (calendar.html) hides the chips a cell has no room for behind "+N more", which opens the day pop-up (`day/`, `_day_posts.html`): the day's posts with time and status, select all / remove selected (the shared bulk selection), and a click on a post shows its preview card. Hovering a future day shows a round "+" in its bottom-right corner.
- Beside the month, week and day grids and the list, on wide screens, a side panel (`templates/calendar/partials/_side_panel.html`) shows the summary of the month being shown (posts, special dates, posts per platform) and the next posts due (`_side_panel_data` / `_upcoming_posts` in `views.py`, both under the calendar's filters). A day's "+" opens a menu: schedule a post (composer with that date), mark a campaign (the "New campaign" modal, prefilled) or add an idea (the sidebar's idea modal, via the `open-create-idea` window event).
- Clicking a post in month/week/day opens its card beside it, its bottom lined up with the post's so it opens upwards (`post-card/<platform_post_id>/`, `templates/calendar/partials/_post_card.html`): preview with the format's own media, type, created/scheduled/published dates, format with its recommended proportion (`PlatformPost.recommended_ratio`: 9:16 for Story/Reels, the platform's own for the feed post), status, and the actions Edit / Clone / Set up automation as left-aligned rows. Ctrl/cmd-click still opens the editor.
- List mode: the Publish tabs (queue / drafts / approvals / sent, `views._get_tab_context`) list only the month shown (`_list_month_bounds`; rows with no date at all stay in every month), and their counts follow the same filters. Queue, drafts and sent share one table (`_list_table.html` / `_list_row.html`: publication, channel, format, date and time, status, actions), grouped by day for queue and sent. The side panel sits beside it and each tab response refreshes it out-of-band (`_tab_counts_oob.html`).
- **Posting slots** (`PostingSlot`): recurring weekly time slots per account.
- **Queues** (`Queue`, `QueueEntry`): named queues that put posts into the next free slot. Entries can be reordered, removed and re-slotted.
- **Recurring posts** (`RecurrenceRule`: daily / weekly / monthly, interval, end date). The posts are generated by `calendar/tasks.py`, 90 days ahead.
- **Campaigns** (`CustomCalendarEvent`; the UI calls them campaigns): the period, start to end, of a campaign or action, drawn as a bar on the month grid; one-off or repeating every month / every year (`recurrence`; occurrences are computed per period by `occurrences()` / `services.events_by_date`, no row per repetition). Created from a day's "+" → "Mark campaign", which opens the "New campaign" modal (`event_form.html`, `events/create/`; a refused form shows an error toast and the modal stays open). A click on a campaign bar opens the same modal in edit mode, with "Delete campaign" (`events/<id>/edit/`, `events/<id>/delete/`); for a repeating campaign it edits the whole series. The toolbar's "Campaigns" button shows or hides them on the calendar for the whole workspace (`WorkspaceSetting` key `calendar.show_custom_events`, `events/toggle/`); the idea board's "Upcoming dates" keeps listing them. The month summary's "Special dates" counts only the special dates, not campaigns.
- **Special dates**: Brazilian holidays and commercial dates (fixed, Easter-based and nth-weekday ones such as Mother's Day and Black Friday) in `holidays.py`, translatable. Shown on the month grid and in the idea board's "Upcoming dates" unless the workspace turns them off (toolbar button; `WorkspaceSetting` key `calendar.show_special_dates`).
- **Board mode** (`?mode=board`, `status_board.py`, `publish_board_shell.html` + `_board_columns.html`): the same toolbar and platform pills as the list, then every `PlatformPost` of the month shown (plus undated ones) in a column by stage (draft, in approval, approved, scheduled, published, failed/rejected). Cards show media, title, caption, platform, format and date; a column shows five and folds the rest under "+N more", and ends with a shortcut (new draft, send for review, schedule post, see all published). Filters reload only the columns (`_columns=1`). A failed card offers "Try again" (a move to Scheduled, so it needs `publish_directly`). Dropping a card runs the existing service for that move — `submit_for_review`, `approve_post`, the bulk "back to draft", or scheduling when the post already has a future time — so permissions, approval records and notifications are the same as elsewhere; refused moves explain why in a toast.

### Tasks (not in spec) — `apps/tasks/`
- One Kanban board per workspace (`TaskColumn`, seeded To do / In progress / In review / Done; renamable, reorderable). Cards (`Task`) have assignees, due date, priority, a checklist (`TaskChecklistItem`) with progress, and links to an idea / post / calendar event. Filters: my tasks, a member, priority. A card shows the first image in its checklist as a cover and the first two lines of its description; a plain checklist item can carry a reference image (preview + download in the task). A task with a draft post shows its **post date** next to the due date: it sets the draft's `proposed_publish_at` (workspace time zone), which places the draft on the calendar and pre-fills the composer's schedule; read-only once the post is scheduled.
- Visible to every member except clients; changing it needs `create_posts`, delivering art needs `upload_media`. Assignees are limited to members who can create content.
- **Art requests** (`request-arts/`, opened as a modal from the board, from an idea and from a Radar offer — `render_art_request_form` lets another app reuse the form with its own action, hidden fields and source note — `_request_arts_host.html`; the networks are picked with the composer's destination cards, `templates/partials/_target_card.html`; networks not connected are offered too): pick formats by network from `creative_formats.py` (feed, Stories, Reels, covers, ads, thumbnails… with exact sizes; some are art-only). Creates one task with a checklist item per format, a briefing for the designers (`description`) and, apart from it, the caption the post will go out with (`post_caption`, prefilled from the idea's text), assigned to the chosen people or the workspace's default (`TaskSettings`), due `art_lead_days` before the idea's date.
- Each art item takes the delivered file (into the media library, counted in the storage quota); a file whose proportions don't match the format is accepted with a warning. When every art is delivered the task moves to the review column and its author is notified. "Create post with these arts" makes a draft with that caption, whose media is the first feed art, with every other publishable art standing in for it on its own format/account through `platform_specific_media` + `derived_from`.
- Notifications: `task_assigned`, `task_ready_for_review`, `task_due_soon` (hourly `send_task_due_reminders`, once per due date, the day before).

### F-2.4 Publishing engine — `apps/publisher/engine.py`
- `PublishEngine.poll_and_publish` finds due `scheduled` PlatformPosts, moves them to `publishing`, and dispatches them in parallel. Concurrency is bounded per post, per platform and globally (`infra.max_concurrent_publish_jobs`), and publishing capacity is reserved in the shared worker.
- Retries use exponential backoff (default 3 attempts). Non-retryable errors fail at once with a readable message (`apps/social_accounts/error_messages.py`), and a `post_failed` notification is sent.
- Publish confirmation: posts that publish asynchronously (for example video processing) are polled until confirmed, and the engine gives up on stale ones (`confirm_pending_publishes`).
- After a publish confirms, the engine posts the first comment (2-minute delay by default) and arms any comment automation attached to the post.
- `PublishLog` keeps a publish audit log for 90 days, and `RateLimitState` tracks per-account upstream rate limits.
- `apps/common/quota.py` is a shared circuit breaker for platform quotas, mainly for YouTube's per-client Data API budget.
- Posts that can only fail are refused at schedule time.

### F-2.5 Social account connection — `apps/social_accounts/`
- OAuth connect with PKCE where supported (`oauth_pkce.py`) and an account picker for Pages and locations, including Google Business location selection (`account_select.html`: search, category filter, follower counts; it says how many new accounts still fit under `MAX_ACCOUNTS_PER_PLATFORM` and stops new ticks there, while accounts already connected can always be ticked to refresh). Bluesky (app password), Mastodon (instance plus a per-instance app registration, `MastodonAppRegistration`) and DEV.to (API key) connect through forms.
- `SocialAccount` tracks a connection status (connected / token_expiring / disconnected / error) and a separate analytics status.
- Token refresh runs ahead of expiry (`token_refresh.py`), and there is a periodic health check. Accounts can be reconnected, have their webhooks retried, or be disconnected, which revokes the token upstream where the platform supports it.
- Pressing Connect first opens a guide for that platform (`partials/_connect_guide.html`; copy in `catalog.py`: three steps and the requirement that most often makes the connection fail, such as a professional Instagram account or a LinkedIn super admin). Platforms with a form of their own (Bluesky, Mastodon, DEV.to) have none.
- The platform's sign-in opens in a popup (`partials/_oauth_popup.html`), for Connect and Reconnect alike; on touch devices, narrow screens or when the browser blocks the window it falls back to the full-page redirect. `views.finish_in_popup` ends the round trip on `connect_done.html`, which hands the next URL to the page over a `BroadcastChannel` (the app's Cross-Origin-Opener-Policy cuts the opener link) and closes.
- A new connection starts with an empty inbox; the account's menu offers "Import history" to bring in the comments and conversations from before it was connected, or, once they are there, "Remove old history" to delete them again; the card shows the job running and a toast says how many messages came or went (see architecture §8). Follower counts and metrics are not affected, and post history has its own offer on the analytics page.
- Meta webhook subscriptions are managed in `social_accounts/webhooks.py`.
- Threads' uninstall and data-deletion callbacks (`platform_callbacks.py`): removing the app in Threads marks the connection as needing a reconnect; a deletion request removes it with its data and gives the person a confirmation code to check on the public data-deletion page. The Facebook app's deletion callback is a POST to that same page and removes the Pages and Instagram profiles that Facebook user granted.
- The Social Accounts page (`social_accounts:list`; `social_accounts:connect` renders the same page on GET and starts OAuth on POST) shows stat cards, filter tabs (all / connected / available / with problems), search and a category filter, one card per account, the platforms still connectable (`apps/social_accounts/catalog.py` gives each its description and category) and the opportunity sources. An account's state is `SocialAccount.health` (ok / warning / error): a warning or an error turns the status pill into a button that opens a modal with the explanation and the way out (reconnect, retry webhooks, disconnect). Posting schedule and disconnect sit behind "Manage connection".

---

### Radar (not in spec) — `apps/radar/`

Opportunities to share, from sources connected on the Social Accounts page (the "Business & opportunities" section below the social platforms; the connect form opens in a modal). A workspace connects at most one account per source. A connected source shows in its card as an account row like a social platform's, with Disconnect at the foot of the card (`social_accounts/partials/_disconnect_button.html`, the same button and confirmation every connected platform and account card uses), on Social Accounts and in the Radar (sidebar, for members with `create_posts`). Connecting and disconnecting need `manage_social_accounts`.

- **Coming soon:** Shein, Mercado Livre and TikTok Shop are listed in `apps/radar/sources.UPCOMING` and show as "Soon" cards, with no action, in Social Accounts and the Radar. Building one means moving it to `SOURCES` with its connection, client and screens.
- **Shopee Affiliates:** App ID + Secret from the Shopee affiliate Open API panel, checked against Shopee before saving; one connection per workspace. The offers screen searches by keyword and niche (Shopee level-1 categories), orders by commission, discount, sales, rating or price, and offers shortcuts (on sale, top rated, up to R$ 20/50/100…) and advanced filters (price, commission %, discount, rating, sold). "Load more" adds pages of 50. Each card shows the commission rate and the estimated earning; "Share offer" opens a dialog with the product's price, commission and stats; video searches for similar products on TikTok, YouTube, Instagram and Pinterest (suggested terms from the title, editable; the term is also copied for apps that open without it); three ready-made texts carrying the affiliate link. The text is editable, then turned into a task (**Create task** closes the dialog and opens the board's art request form filled from the offer — title, brief with price · discount · commission and the links, caption with the chosen text — where the user picks the formats; the task gets an item per format plus the product image, copied to the media library, and the draft post with the text and that image; then the board opens on it) or opened as a draft in the composer to schedule (**Schedule post**).
- **Favorites:** the star on a card saves the offer as a snapshot; the Favorites tab lists them even after Shopee stops returning the offer (prices shown are from when it was saved).
- **Commissions and orders:** a Shopee Affiliates connection is also an account in Analytics (see F-4.1); the offers screen links to it.
- **Instagram research** (`radar/instagram/`, `apps/radar/instagram.py`; a card on the Radar index when the workspace has an Instagram account connected with Facebook Login). Read through that account (a picker when there are several), nothing stored but the handles:
  - *Watched profiles* (`WatchedProfile`, one per workspace and handle): each opens through Business Discovery with followers, posts, interactions per post (likes + comments over the last 12 posts, leaving out posts that hide likes), engagement rate and the latest posts. Only business and creator accounts answer.
  - *Hashtags*: the most popular or most recent public posts for one hashtag. Needs the Instagram Public Content Access feature approved in Meta's App Review (until then the screen says so), and Meta counts 30 distinct hashtags per account in a rolling 7 days.

## 3. Inbox & engagement

### F-3.1 Unified inbox — `apps/inbox/`
- `InboxMessage` types: comment, mention, tag, DM, review. Statuses: unread, open, resolved, archived.
- Instagram mentions and tags (Facebook Login connection): the `mentions` webhook names a mention only by id, so it is filed with a placeholder and `fill_instagram_mention` (a background task) replaces it with the text, author and link read through `mentioned_comment` / `mentioned_media`. A reply to a mention goes through the account's `/mentions` edge, since the media is another account's. Posts that tag the account in the photo have no webhook: the poll reads the `/tags` edge and files them as `tag`, shown without a reply box because Meta lets no one answer them.
- Messages arrive two ways:
  - Webhooks at `/webhooks/facebook/`, `/webhooks/instagram_login/` and `/webhooks/youtube/`. Meta requests are signature-checked.
  - The polling sync engine (`tasks.py` `InboxSyncEngine`, every 5 min by default), with historical backfill. YouTube polling uses bounded, resumable history walks to save quota.
- Replies follow a draft → send lifecycle (`InboxReply`: draft / sent / failed). The inbox UI, the REST API and MCP all go through the same layer (`services.py`).
- **DMs** (Facebook Messenger, Instagram via Facebook Login, Instagram Direct, WhatsApp Cloud API). WhatsApp is connected on the Social Accounts page through Meta's Embedded Signup and is a messaging-only channel: it never appears in the composer, calendar, analytics or automations. The account cards of the chat channels show whether private messages are reaching the inbox ("Messages active" / "Messages unavailable"). Send eligibility is in `reply_policy.py`, which enforces Meta's messaging windows: a standard reply within 24 h of the customer's last message, and a human-agent reply up to 7 days. Outside those windows sending is blocked with a reason (translated), which the inbox shows in place of the reply box, with a lock. Instagram DM OAuth scopes are controlled by `PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED` (on by default).
- Assignment, internal notes (`InternalNote`), saved replies (`SavedReply`), bulk actions, and auto-resolve on reply.
- One inbox for every channel (the sidebar has no per-channel shortcut; WhatsApp is one of the platform filters). The page (`templates/inbox/feed.html`) fills the area beside the sidebar like the flow builder, without cards: a top bar with the title and the inbox's sections (`_inbox_tabs.html`: Conversations — this page —, Funnel — `apps/funnels` — and Contacts) (saved replies open from the reply box's "+"), a filter row (`_filter_bar.html`: the calendar's channel pills — "All" and one per connected platform —, tabs for direct messages ("Inbox"), comments and mentions with their unread count under the channel (a second click shows every type again), and search), then three columns split by rules: the entries — one per conversation (`inbox_services.thread_key`: the same person, account and kind, and for comments the same post; direct messages, comments and mentions stay apart), showing its latest message, how many messages it holds, whether any is unread and the origin of its post (Feed / Story / Reels: `InboxMessage.origin`, from the Connekt Flow post or `extra["post_origin"]`, which the Instagram comment poll and the first read of a post's preview record) — with their count and sort (newest / oldest), the conversation (`_message_panel.html`: the post it hangs off, the messages, the reply box with the first saved replies as shortcuts) and, from 2xl, "Entry details" (`_message_details.html`: channel, type, origin, post, status, sentiment, assignee, "Mark as resolved"); below 2xl status and assignee move to the conversation header. Opening an entry shows the whole conversation and reads it, as a WhatsApp-style chat (`views._chat`, `_chat_incoming.html`, `_reply_item.html`, `_note_item.html`): day separators, the customer on the left in white, the team's replies on the right with who sent them, internal notes on the right in amber, the time (workspace clock) inside each bubble, opened at the latest message; status, assignee and bulk actions apply to the whole conversation. The post a comment hangs off (`inbox_services.post_preview`: caption, image, link and origin Feed / Story / Reels) is read locally for posts published through Connekt Flow and otherwise from the platform (`get_post_preview` on the Instagram and Facebook providers), cached for 6 hours; a refused read says so on the entry. A tag or mention shows the other account's post from what the sync recorded (the Instagram `/tags` poll stores its picture and kind in `extra["post_image"]` / `extra["post_origin"]`, refreshed every cycle as the CDN links expire). The post card opens the chat, on the chat's background, and the person's later messages follow it.
- The list refreshes itself within seconds when a message arrives (`inbox:version` poll). The reply box has an emoji picker (the composer's catalogue, `static/js/emoji-picker.js`), searchable saved replies with their variables filled in, an "insert name" button, Enter to send / Shift+Enter for a new line, a per-platform character counter, and the time the reply window closes (`static/js/inbox-reply.js`).
- Sentiment: a keyword-based classifier (`sentiment.py`) labels messages positive / neutral / negative, and a user can override it manually.
- SLA (`InboxSLAConfig`): a response-time target per workspace, with an `inbox_sla_overdue` notification.

### Funnels — `apps/funnels/`
- A workspace has funnels (`Funnel`), each an ordered list of stages (`FunnelStage`: name, colour, kind open / won / lost). The main funnel ("Vendas - Principal", with Novo lead, Qualificação, Proposta, Negociação and Convertido) is created on first use (`services.funnels_for`); its names are written in the language of whoever opens it first.
- An inbox conversation (the inbox's `thread_key`) sits in at most one funnel as an `Opportunity` (stage, value, interest, tags, notes, the person's name). A conversation with no opportunity counts as the first stage of the main funnel, so no row is written until someone moves or edits it (`services.ensure_opportunity`). Moves are recorded as `OpportunityActivity`.
- The inbox's "Funnel" section (`funnels:inbox`) is the inbox page (`inbox.views.feed_context` with a funnel scope) with a funnel bar (active funnel, stages with their count, "See the whole funnel"), each entry's stage, and "Opportunity details" in place of "Entry details" (`_opportunity_details.html`, opened with `mode=funnel`, which the panel passes on through an inherited `hx-vals`): contact (linked to Contacts when matched), funnel, stage, status, assignee, tags, interest, potential value, notes, "Move to <next stage>". Its composer has an "Automation" tab that runs the post's flow for a comment (`automations.actions.start_flow_for_message`: the same 7-day window, claim and log as an automatic run).
- The board (`funnels:board`): figures (conversations, active opportunities, converted, conversion rate, potential value), filters (funnel, channel, assignee, tag, origin, search, period), one column per stage with its count and value, cards dragged between stages (`funnels:board_move`), "Add opportunity" (conversations not in the funnel yet), funnel performance and recent activity.
- Settings (`funnels:settings`, `manage_workspace_settings`): create and rename funnels, add, edit, reorder and delete stages. Deleting a stage moves its conversations to the funnel's first stage; deleting a funnel sends them back to the main funnel; the main funnel cannot be deleted.

### Automations (comment-to-reply / comment-to-DM) — `apps/automations/`
Not in the original spec. It is adapted from BrightBean Chat and runs on **Facebook, Instagram and Instagram Direct** comments only (`matching.COMMENT_PLATFORMS`). In the "new rule" chooser, Google Business is shown as "coming soon".

- **Rules** (`AutomationRule`) belong to one account, or to all compatible accounts when no account is set.
  - Matching: include/exclude keywords with five modes (`keywords.py`): exact, contains, any_word, and accent-insensitive contains_loose / any_word_loose. Rules can be scoped to all posts or to specific posts, and to top-level comments only.
  - Rules are evaluated in priority order and the first match wins (`matching.py`).
  - Replies: a public comment reply and/or a private reply (DM). Each uses a fixed text or a random variation. The `ai` mode is declared but inert.
  - Guards (`guards.py`): once per sender per post, `max_per_hour` (default 30), and the account's own comments are skipped. Every decision is logged in `AutomationLog`, with outcomes sent / skipped_* / no_match / failed.
  - New rules start disabled. A rule has a "test" action, and the log is at `automations/log/`.
- **Flow builder** (`flow_views.py`, `flow.py`, `engine.py`, `templates/automations/flow_builder.html`): a canvas of nodes and edges.
  - Node types: `trigger_comment`, `reply_comment`, `send_dm`, `send_card` (image card, picked from the media library), and `condition`. A condition branches on comment text or on the sender's profile: follows us, we follow them, verified, follower count.
  - Messages can carry buttons. A postback button parks the run in `FlowRun`, keyed by account and sender, until the person taps it.
  - Execution: `flow.py` compiles the canvas into the rule's fields. `engine.py` walks the graph, sends at most 5 messages per wake (`MAX_STEPS_PER_WAKE`), stops cycles, and abandons the run on the first send failure.
- **Per-post flows** (`PostAutomation`): a flow is bound one-to-one to a `PlatformPost`. If the post is still a draft or scheduled, the flow waits as `pending`. The publisher arms it (`services.arm_for_platform_post`) when the post is confirmed live, and on an already-published post it arms on save. Existing scheduled or published posts can be chosen from `flow_choose_posts`.
- **Flow templates** (`FlowTemplate`): a global library edited by staff at `/automations/staff/templates/` (create, edit canvas, details, thumbnail, duplicate, publish, reorder). Authors start a flow from a published template through `templates/`. The graph is copied, so later edits to the template do not reach flows already created from it.

### Contacts (not in spec) — `apps/contacts/`
People and companies the workspace talks to (sidebar: Engajar → Contatos, for members with `use_inbox`).

- **Where contacts come from:** created by hand (`contacts:create`), imported from a CSV with the export's columns (`contacts:import`; only `name` is required, a row whose WhatsApp, Instagram or email already belongs to a contact is skipped, 5,000 rows / 2 MB), or by the Inbox: the first message from a sender creates a `Contact` with a `ContactChannel` holding the sender's platform ID (`services.on_inbox_message_created`, from the same two places as the automations hook). A later message from the same person lands on the same contact through that ID, or through the channel's value when the person arrives under their @handle or was typed in by hand. A Facebook DM is a Messenger channel; a Facebook comment is a Facebook channel.
- **Engagement:** `interactions_90d` counts the inbox messages the contact sent in the last 90 days (matched through its channels, `services.interactions_queryset`); 5 or more is high, 2–4 medium, fewer low. A level chosen in the form (`engagement_manual`) wins. The daily `refresh_contacts` task creates contacts for senders the live hook missed (any date, so its first run imports the history), recounts recent senders and lets quiet contacts decay.
- **List** (`contacts:list`): cards (total, active, funnels in use, new in 30 days, engagement rate = share of high or medium), channel chips, search and filters (status, source, funnel, tag, segment, engagement, last contact) swapped over HTMX and kept in the URL, pagination, a floating selection bar (delete, and merge for two or more: the person picks the contact that stays, `services.merge_contacts` moves the others' channels with their inbox link, joins tags and notes, fills its empty fields and deletes them), an inline funnel select (`contacts:set_funnel`) and CSV export of the filtered list (cells that start like a formula are escaped).
- **Form:** personal data (CPF/CNPJ with check digits, date of birth, gender, company, photo up to 2 MB), channels (one is the main one), status, funnel, tags, segment, source, engagement and address; the edit page also lists the contact's Inbox interactions.
- **In the Inbox:** the open entry's details column shows the sender's contact (`services.contact_for_message`, read-only): source, tags, funnel, engagement, last contact and a "View profile" link.
- **Not built yet:** the Funnels, Lists, Segments and Campaigns tabs show as "Coming soon". Funnel and segment are free text on the contact (with suggestions from the values in use) until those tabs give them their own models.

---

## 4. Analytics — `apps/analytics/`

### F-4.1 Per-account and per-post analytics
- Snapshot tables: `AccountInsightsSnapshot` (daily account metrics), `PostInsightsSnapshot`, and `YouTubePeriodReport` (native YouTube reports, cache-only for display).
- The metric catalog for each platform is in `metrics.py`. Engagement rate and period deltas are computed in `derive.py`, and values the product calculates itself are labelled as estimates.
- Pages: workspace overview (`index`), per account (KPI cards, 7/30/90-day trend charts, sortable post table), and post detail.
- Sync cadence (`tasks.py`):
  - On connect: a backfill.
  - Account metrics: daily.
  - Post metrics: hourly for posts under 24 h old, every 6 h up to 7 days, daily up to 30 days, weekly up to 90 days, then no more syncing.
  - Days with no activity are stored as zeros. Days the platform did not report are drawn as gaps.
- Instagram analytics have their own access check, separate from Page publishing (`instagram_access.py`). Analytics are paused when a provider quota is hit (`ProviderQuotaBlock`).
- **Shopee Affiliates** (`shopee/<connection_id>/`, view `shopee_report`, numbers from `apps/radar/reports.py`): listed in the account switcher under "Affiliate sales", and the landing page when the workspace has no channel. Same layout: commission, orders, sales and items sold cards with deltas, commission per order with orders by status, the trend chart, top products, where sales came from (Shopee's `referrer`) and the orders table filtered by status. Read live from Shopee's `conversionReport` (cached 15 minutes, nothing stored); pending orders count, cancelled ones only as cancelled; days are the workspace's. Shopee only answers for the last 90 days, so the 90-day range has no comparison.

### History import (not in spec) — `history_import.py`
- From an account's analytics page (`<account_id>/history/`), the user can import posts the channel already had. The import uses `SocialProvider.list_existing_posts`. Imported posts become ordinary `PlatformPost` rows, marked in `platform_extra`, and the normal sync then collects their metrics.
- The account chart is also backfilled for the days before the connection, where Meta's API returns a daily series: Facebook page insights (all four metrics) and Instagram reach. Days the product already measured, and today, are never overwritten.

---

## 5. Platform configuration

### F-5.1 Authentication — `apps/accounts/`
- Email/password signup and login through django-allauth, Google SSO (`GOOGLE_AUTH_CLIENT_ID`), terms acceptance, and an account settings page.
- `SIGNUP_MODE` = `open` | `invite` | `closed`. `CUSTOM_SIGNUP_URL` replaces every "Sign up" link and, in `invite` or `closed` mode, is where blocked signups are sent (`signup_links.py`, `adapters.py`).
- Auth POSTs are rate-limited per IP: 10 attempts per 60 s (`apps/accounts/middleware.py`). Sessions last 14 days (`SESSION_COOKIE_AGE`).
- 2FA: the TOTP fields exist on `User`, but there is no setup or enforcement flow yet.
- **Operator impersonation** (`impersonation.py`, `views_impersonation.py`): staff can sign in as a customer for support. Staff cannot impersonate other staff. Start and stop are POST-only, and every session is recorded in `ImpersonationSession`, which can be viewed in the staff console.

### F-5.2 Branding
- Per-workspace logo/icon and primary/secondary colours (`Workspace`), plus the organization logo URL. There is no custom domain or email white-labelling.

### F-5.3 Integrations
- Unsplash stock search and import in the composer.
- Outgoing webhooks are a notification channel (see F-7.1).
- The REST API and MCP server are described below. There are no other design, communication or storage integrations.

### Agent API — `apps/api/`, `apps/api_keys/`
- REST API built with Django Ninja at `/api/v1/`, with OpenAPI docs at `/api/v1/docs`. Routers: `me`, `accounts`, `posts` (create, get, patch, schedule, cancel), `media` (upload, get), `analytics` (account, post), `inbox` (list, get, reply drafts, send).
- **API keys** (`ApiKey`, managed at `/organizations/api-keys/`, requires `manage_api_keys`):
  - A key belongs to one workspace and has an explicit allowlist of social accounts and a subset of permission keys. On each request its permissions are intersected with the issuer's current permissions, so the key stops working if the issuer loses membership.
  - Token format is `bb_studio_<secret>_<lookup>`. Only an HMAC hash of the secret is stored. Keys can have an expiry and can be revoked or edited.
  - `ApiKeyAuditLog` records audited actions.
- Limits (`limits.py`): per-key, per-workspace and per-IP throttles; hourly and daily call caps counted in `ApiKeyUsageHourly`; and per-platform daily post caps per account (`PLATFORM_DAILY_POST_LIMIT`, e.g. Instagram 25, TikTok 15, Threads 250). Writes accept idempotency keys (`IdempotencyRecord`).

### MCP server — `apps/mcp/`
- Streamable HTTP MCP endpoint (POST only, tools only) at `/api/v1/mcp`. It accepts the same bearer API keys, or OAuth tokens (below).
- 20 tools (`handlers.py`):
  - Posts: list_accounts, create_draft, schedule_post, schedule_draft, get_post, list_posts, cancel_post.
  - Media: search_media, get_media, upload_media, request_media_upload, finalize_media_upload.
  - Analytics: get_account_analytics, get_post_analytics.
  - Inbox: list_inbox_messages, get_inbox_message, create_reply_draft, update_reply_draft, discard_reply_draft, send_reply.

### OAuth server for MCP clients — `apps/oauth_server/`
- An OAuth 2.1 authorization server (django-oauth-toolkit) so MCP clients such as Claude Desktop can sign users in directly. Endpoints: `/oauth/authorize/`, `/oauth/token/`, `/oauth/revoke_token/`.
- Open Dynamic Client Registration (`/oauth/register`, RFC 7591, rate-limited), with metadata documents under `/.well-known/`.
- PKCE is required and only `S256` is accepted (`validator.py`).

### Intelligence — `apps/intelligence/`
- An optional integration with the separate BrightBean Intelligence service (third-party, run by upstream). It is enabled only when `INTELLIGENCE_INTERNAL_URL`, `INTELLIGENCE_PUBLIC_URL` and the related secrets are set, otherwise every Intelligence surface is hidden.
- Org-scoped at `/orgs/<org_id>/intelligence/`.
  - Subscription: plan picker, checkout through Intelligence (Stripe), recovery after a closed tab, portal, billing contact. State is kept in `IntelligenceSubscription`, `StudioCheckoutAttempt` and `PendingActivation`.
  - Tools playground: score packaging, score video hook, benchmark channel or video, research content gaps, list niches.
- Calls go through `services/client.py`, which signs internal calls with HMAC and uses a bearer key per org for tool calls. Using the tools needs `use_intelligence` and an active subscription. The Intelligence plan also sets the org storage tier (`STORAGE_QUOTA_TIERS`).

---

## 6. Media library — `apps/media_library/`

### F-6.1
- Workspace libraries (`/workspace/<id>/media/`) and a shared org library (`/organizations/media/shared/`).
- Nested folders, tags with autocomplete, starring, search, move, download, and alt text, title and attribution fields.
- The composer's media picker lists the folders, creates top-level ones ("New folder", for whoever has `manage_media`) and uploads into the folder it has open. Folder names are unique among siblings, also at the top level (`services.create_folder` checks it: the database constraint can't, since PostgreSQL never treats two NULL parents as equal).
- Background processing (`tasks.py`) creates thumbnails and platform-optimised variants (`processed_variants`) and reports processing status. Images can be edited and videos trimmed. Each edit is a `MediaAssetVersion`, and earlier versions can be restored.
- Large uploads go through `PendingUpload` (a direct-upload handshake also used by the API and MCP). Orphaned uploads are swept.
- Storage limits, both of which must pass (`quotas.py`):
  - Org cap: an infrastructure guard, 5 GB by default or the Intelligence tier.
  - Workspace cap: the billed quota (see Billing).

---

## 7. Notifications — `apps/notifications/`

### F-7.1
- Channels: in-app, email, and webhook (HTTP POST with an HMAC-SHA256 signature).
- Events (`EventType`): post submitted, approved, changes requested, rejected, published and failed; new inbox message; inbox SLA overdue; client approval requested; team member invited; social account disconnected; comment mention; approval reminder; stalled approval; client hold requested; client connected accounts. `report_generated` and `engagement_alert` are defined but nothing sends them.
- Per-user preferences for each event and channel, quiet hours (`QuietHours`), digest/batched email, an in-app drawer with an unread count, and unsubscribe links in emails.
- Delivery attempts are tracked with a retry cap (`NotificationDelivery`).
- Outgoing email is rate-limited (`EmailSendCounter`). Bounces and complaints add the address to a suppression list (`EmailSuppression`) through the Resend webhook at `/webhooks/resend/` (`apps/common/webhooks.py`).

---

## Billing (not in spec) — `apps/billing/`

- The billable unit is the **workspace**. There is one plan with every feature, priced per workspace per month: R$ 50,00 (`BILLING_WORKSPACE_PRICE_CENTS`), with 10 GB of disk included. Extra disk is sold as `WorkspaceStorageAddon` rows, at R$ 2,00 per GB per month by default (a placeholder price). Money is stored in integer cents and disk in bytes.
- Gateway: **Asaas** (`gateways/asaas.py`; `ASAAS_API_KEY`, `ASAAS_API_BASE` defaults to sandbox).
  - The Organization is the Asaas customer, and each workspace is its own Asaas subscription (`contracting.py`). Contracting is idempotent.
  - Subscribing (`/workspace/<id>/billing/`) leaves the subscription `pending`. Only the Asaas webhook (`/webhooks/asaas/`, authenticated by the `ASAAS_WEBHOOK_TOKEN` header) makes it `active`. The webhook maps payment confirmed/received to active, overdue, refund or chargeback to past_due, and subscription deleted to canceled. Events are deduplicated in `BillingEvent`.
- Courtesy (comp) access: per workspace (`courtesy` status) or per account (`OrganizationBillingProfile.courtesy_workspace_quota` with an optional expiry date, expired by `tasks.expire_courtesies`).
- Disk enforcement (`storage_usage.py`): a warning banner at 90% in the media library and composer, and uploads blocked at 100%.
- The composer's media picker always shows a compact disk meter in its footer (`templates/billing/_storage_meter.html`: used of allowance, % and what is left), refreshed after an upload; hidden for a workspace without a subscription.
- Paid status is answered by `services.is_workspace_active`. Today it gates only storage. Other features do not yet check it.
- **Staff console** (`/billing/staff/`, `is_staff` only, with its own admin layout):
  - Customer accounts table: grant or revoke courtesy, set the courtesy quota, edit the account, send a password reset, activate or deactivate users, impersonate.
  - Plans and prices: editable at runtime, stored in `ProductPrice` with a history in `ProductPriceChange`, and override the env defaults.
  - Subscription list with counts, and the impersonation log.

---

## Operational and compliance

- Public home page at `/` for visitors who are not signed in (`legal_views.home`, `templates/legal/home.html`): what the product does and how it uses connected accounts' data, with links to the legal pages. Google's OAuth brand verification and Meta's App Review open the root anonymously; a signed-in user is sent to their workspace as before.
- Public legal pages for Meta App Review: `/privacidade/`, `/termos/` and `/exclusao-de-dados/` (`apps/common/legal_views.py`); the English `/privacy/`, `/terms/` and `/data-deletion/` still answer. `/robots.txt` (same module) allows Meta's crawlers by name, which read these pages and the media of every post.
- YouTube compliance (`youtube_compliance.py`, `YouTubeRevocation`): handles grant revocation and the retention lifecycle of YouTube data.
- Field-level encryption of tokens and credentials (`apps/common/encryption.py`, keyed by `ENCRYPTION_KEY_SALT`).
- `/health/` endpoint. Stale task locks are released when the worker starts.

---

## Not implemented from the original spec

- F-1.3 Custom roles UI (the model exists but is admin-only).
- F-1.4 Client portal reports (the page is a placeholder) and per-portal branding.
- F-1.6 Settings UI for org/workspace defaults (overrides only through admin).
- F-1.7 "First content batch" onboarding step.
- F-2.1 AI writing assist in the composer.
- F-2.3 Optimal posting-time suggestions (only the setting keys exist).
- F-4.2 Cross-account / cross-workspace analytics dashboard and engagement alerts.
- F-4.3 Report builder (sections, templates, PDF export, scheduled reports, share links).
- F-5.1 Two-factor authentication (TOTP fields only; no setup or enforcement).
- F-5.2 Custom domain and email white-labelling.
- F-5.3 Design tools (e.g. Canva), communication (Slack/Teams), and external storage integrations.
- F-8.1 Global search.
- F-8.3 Post trash / soft delete with restore.
- F-8.4 GDPR data export (ZIP).
- F-8.6 Link shortening, UTM management and link-click tracking.
- F-8.7 Category analytics (categories and their calendar colours exist).
