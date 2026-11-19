# Technical Architecture

How the codebase is put together today. It replaces the original v1 plan (`development_specs/architecture.md`, now only in git history). When this file and the code disagree, the code is right. Please fix this file.

---

## 1. Stack

| Layer | Technology |
|-------|-----------|
| Backend | Django 5.1 (`requirements.txt`), Python 3.12 (`Dockerfile`) |
| Frontend | Django templates, HTMX (`django-htmx`), Alpine.js, vendored in `static/js/` |
| CSS | Tailwind CSS 4 via `django-tailwind` (`theme/`) |
| Database | PostgreSQL 16. Also holds the job queue. SQLite works for local development only. |
| Background jobs | `django-background-tasks` rows, run by the custom worker `apps/common/worker.py`. No Celery. |
| Cache | LocMemCache by default. Redis is used only if `REDIS_URL` is set (`config/settings/base.py`). |
| Media storage | Local filesystem (default) or S3-compatible storage via `django-storages` |
| Media processing | Pillow (images), FFmpeg (video, installed in the image) |
| Email | SMTP behind a budget wrapper (`apps/common/mail.py`). Resend is used as an SMTP provider and sends bounce webhooks. |
| Agent API | `django-ninja` (REST + OpenAPI), a hand-written MCP transport, `django-oauth-toolkit` |
| Payments | Asaas (workspace billing). Stripe is only reached through the optional Intelligence service. |
| Monitoring | Sentry (`sentry-sdk`), turned on when `SENTRY_DSN` is set |
| Web server | Gunicorn (`--workers 1 --threads 4`) behind Dokploy's Traefik |

---

## 2. Repository Layout

```
config/settings/{base,development,production,test}.py
config/urls.py            # all URL mounts, public media routing, error handlers
apps/                     # Django apps (below)
providers/                # one module per social platform + shared Meta helpers
theme/                    # django-tailwind app; static_src/src/styles.css
templates/                # project templates (base.html, components, ninja/)
static/                   # favicon, images, vendored JS (htmx, alpine, cropper)
locale/pt_BR/             # translations (.po; .mo compiled at image build)
tests/                    # cross-app tests (providers, etc.); apps also have tests/
docker-compose.yml + docker-compose.override.yml   # development
docker-compose.dokploy.yml                         # production
```

| App | Responsibility |
|-----|----------------|
| `accounts` | Custom `User`, allauth adapters, signup modes, ToS gate, auth rate limit, operator impersonation |
| `organizations`, `workspaces` | Tenancy. An Organization owns Workspaces. |
| `members` | Org and workspace memberships, roles, invitations, `RBACMiddleware` |
| `settings_manager` | `OrgSetting` / `WorkspaceSetting` key-value overrides |
| `credentials` | `PlatformCredential` (per-org app credentials) and credential resolution |
| `social_accounts` | OAuth connect flows, `SocialAccount`, health checks, webhook subscription, YouTube compliance |
| `composer` | `Post` / `PlatformPost`, ideas, templates, tags, CSV import, feeds |
| `calendar` | Calendar views, posting slots, queues, recurrence rules, custom events, special dates, status board |
| `tasks` | Team task board, checklists, art requests by creative format |
| `publisher` | Publishing engine (`engine.py`), `PublishLog`, rate-limit state |
| `media_library` | Assets, folders, versions, direct-to-S3 uploads, thumbnails, quotas |
| `approvals` | Approval workflow, post comments, reminders |
| `client_portal` | Magic-link portal for client reviewers |
| `onboarding` | Client connection links, onboarding checklist (not shown on any screen) |
| `inbox` | Unified inbox (comments, mentions, DMs), polling sync, inbound webhooks |
| `automations` | Comment-triggered auto-replies and flows (DM, buttons) |
| `contacts` | Contacts (manual, CSV, created from inbox senders), channels, engagement, filters |
| `analytics` | Account and post metrics, history import |
| `notifications` | In-app, email and webhook notifications, digests |
| `billing` | Per-workspace subscription via Asaas, storage add-ons, staff console |
| `intelligence` | Optional client for the hosted BrightBean Intelligence SaaS (third-party, run by upstream) |
| `api_keys` | `bb_studio_` keys, audit log, hourly usage |
| `api` | Agent REST API (`/api/v1/`) |
| `mcp` | MCP tools and Streamable-HTTP transport (`/api/v1/mcp`) |
| `oauth_server` | OAuth 2.1 discovery + DCR for MCP clients |
| `common` | Shared bits: encrypted fields, scoped managers, worker, mail budget, Resend webhook, legal pages |

---

## 3. Tenancy and Access Control

- **Hierarchy:** `Organization` → `Workspace` → content (posts, accounts, media, inbox). A workspace is also the billable unit (§11).
- **Roles** (`apps/members/models.py`): org roles `owner|admin|member`. Workspace roles `owner|manager|editor|contributor|client|viewer`.
- **`RBACMiddleware`** (`apps/members/middleware.py`) attaches `request.org`, `request.org_membership`, `request.workspace` and `request.workspace_membership`. A user may belong to several organizations (`request.org_memberships`); `request.org` is the one of the workspace in the URL, else of the last workspace opened, else the first joined. It skips `/static/` and `/media/`.
- **Data isolation is explicit, not automatic.** `apps/common/managers.py` provides `OrgScopedManager.for_org()` and `WorkspaceScopedManager.for_workspace()`. Views must call these themselves (or filter by workspace). Nothing filters querysets implicitly, and there is no PostgreSQL row-level security.
- Workspace-scoped URLs follow the pattern `/workspace/<uuid:workspace_id>/...` (`config/urls.py`).
- **Staff-only surfaces** (gated on `is_staff`, outside any workspace prefix): `/billing/staff/` (billing console, courtesy grants, impersonation log), `/automations/staff/templates/` (flow template library), `/composer/staff/ideas/`, `/calendar/staff/dates/`, `/radar/staff/hashtags/`, `/onboarding/staff/videos/` (the page banners' help videos) and `/social-accounts/staff/platforms/`. Each lives in an `apps/<app>/staff_urls.py` module and is the administrative area's menu (`layouts/admin_shell.html`); no membership check stands in front of these paths, so every view checks `is_staff` itself. `tests/test_staff_routes_are_staff_only.py` walks every route of every `staff_urls` module found in the root URLconf and asserts a visitor is sent to log in and a non-staff customer gets 403, GET and POST. The impersonation rules are in `apps/accounts/impersonation.py`.

---

## 4. Frontend

Server-rendered HTML. The only build step is Tailwind. In development it runs as `npm run start` in `theme/static_src` (or the `tailwind` service in `docker-compose.override.yml`). In the image, `npm run build` runs in the `Dockerfile`.

- HTMX handles partial swaps, inline actions and polling. `django_htmx.middleware.HtmxMiddleware` is installed. A page whose part waits on a platform's API or a heavy calculation renders at once and loads that part into a placeholder (`templates/components/_skeleton.html`, `apps.common.htmx.lazy_body_urls`): the Meta Ads pages and settings, the Meta Ads and Shopee reports, the Radar's Shopee offers, the posts table of an account's report and the dashboard's "By network" cards.
- Alpine.js handles client-side UI: dropdowns, modals, drag and drop, character counters. CSP allows `'unsafe-eval'` because the standard Alpine build needs it (`base.py`).
- HTMX and Alpine are vendored (`static/js/htmx.min.js`, `static/js/alpine.min.js`), as is Cropper.js under `static/js/vendor/`. Static files are served by WhiteNoise (`CompressedManifestStaticFilesStorage` in production).
- Confirmations use one dialog, `templates/components/_confirm_dialog.html` (included by `base.html`): it handles every `hx-confirm` through htmx's `htmx:confirm` event and every form with `data-confirm`, so no page opens the browser's `confirm()`.
- Context processors in `base.py` add sidebar data, the unread-notification count, the Intelligence flag, a storage-quota banner and an impersonation banner to every page.

---

## 5. Social Providers

`providers/base.py` defines `HttpProvider` (the shared `_request`, `_error_for_response` and `_safe_json`) and the abstract `SocialProvider` built on it. Required members: `platform_name`, `auth_type`, `max_caption_length`, `supported_post_types`, `supported_media_types`, `required_scopes`, `get_profile`, `publish_post`. Optional members have defaults: OAuth (`get_auth_url`, `exchange_code`, `refresh_token`), `check_publish_status`, comments, metrics (single and batch), account metric series, demographics, `get_messages`, `reply_to_message`, `reply_to_comment`, webhook (un)subscription, `revoke_token` and others. Shared HTTP handling (`_request`, `_poll_until`, error classification) lives in the base. Exceptions are defined in `providers/exceptions.py` and data types in `providers/types.py`.

`PROVIDER_REGISTRY` (`providers/__init__.py`) maps platform keys to classes: `facebook`, `instagram` (via Facebook Login), `instagram_login` (Instagram Login, no Page), `linkedin_personal`, `linkedin_company`, `tiktok`, `youtube`, `pinterest`, `threads`, `bluesky`, `google_business`, `mastodon`, `devto`, `x` (OAuth 2.0 with PKCE; media is uploaded as bytes through the v2 upload endpoints), `whatsapp` (WhatsApp Cloud API, a messaging-only channel — see below). Meta-specific helpers are split into `meta_oauth.py`, `meta_messaging.py`, `meta_comments.py`, `meta_insights.py`, `meta_history.py`, `meta_accounts.py` and `meta_feed_extras.py`. `google_errors.py` handles YouTube quota and reset time.

**Credential resolution** (`apps/credentials/models.py: resolve_platform_credentials`): if the env vars (`PLATFORM_CREDENTIALS_FROM_ENV` in `base.py`) fully configure a platform, they win. Otherwise the org's admin-entered `PlatformCredential` row is used. Bluesky (app passwords), Mastodon (per-instance app registration in `MastodonAppRegistration`) and dev.to (per-account API key) need no app-level credentials.

**Adding a platform:** add a module in `providers/`, register it in `PROVIDER_REGISTRY`, add the platform to the `PlatformCredential` choices and credential settings, then check the platform-specific branches in `social_accounts`, `publisher`, `inbox` and `analytics`. In practice these systems do branch on platform keys.

**Messaging-only platforms** (`MESSAGING_ONLY_PLATFORMS` in `apps/social_accounts/models.py`, today only `whatsapp`) are `SocialAccount`s that nothing is published to. Every list that offers accounts for publishing, scheduling, analytics, automations or API keys goes through `SocialAccount.objects.publishable()`; the inbox, the Social Accounts page and the health check treat them like any other account, and no posting slots are created for them. WhatsApp connects through Meta's **Embedded Signup**: `templates/social_accounts/partials/_whatsapp_signup.html` loads the JS SDK (CSP widened only on the Social Accounts views), runs `FB.login` with `PLATFORM_WHATSAPP_CONFIG_ID`, and posts the code plus the WABA and phone number the browser reports to `social_accounts:connect_whatsapp`. The view exchanges the code, checks the phone number against `/{waba}/phone_numbers` for that token before trusting either id, registers the number on the Cloud API if needed (random 2FA PIN, not kept), and stores the phone number id as `account_platform_id` and the WABA as `webhook_target_id`. The webhook subscription is `POST /{waba}/subscribed_apps`. The Cloud API has no message history endpoint, so WhatsApp is excluded from inbox polling.

**Meta Ads** is not a social provider either. `providers/meta_ads.py` (`MetaAdsClient`, an `HttpProvider`) calls the Marketing API on the Graph host and version of `providers/facebook.py`, and `apps/ads` keeps one `AdsConnection` per workspace (encrypted user token, chosen ad account, Pixel and Page). It signs in with the Facebook app credentials (`resolve_platform_credentials("facebook", …)`) and its own optional `PLATFORM_META_ADS_CONFIG_ID`. Campaigns are read live, never mirrored; every campaign or ad set id from the browser is checked against the connected ad account before use. Budgets travel in the currency's minor unit (`to_minor_units`), and campaign creation sends `is_adset_budget_sharing_enabled=false` and `targeting_automation.advantage_audience=0`, both required by recent Graph versions.

**Opportunity sources (Radar)** are not social providers: nothing is published to them, so they stay out of `PROVIDER_REGISTRY` and out of `SocialAccount`, which the composer, publisher, inbox and analytics all iterate. `apps/radar` stores one `OpportunityConnection` per workspace and source (credentials in an `EncryptedJSONField`) and `apps/radar/sources.py` holds the registry that the Platforms connect grid, the connected-accounts list and the Radar index render from. The first source is Shopee Affiliates (`providers/shopee_affiliate.py`, an `HttpProvider`): one GraphQL endpoint per region, fixed in code, every request signed with `SHA256(app_id + timestamp + payload + secret)`. Shopee answers HTTP 200 with `errors[].extensions.code` on failure; the client maps `10020` to `OAuthError`, `10030` to `RateLimitError`, `10031`–`10035` to a non-retryable 403 `APIError`, and retries its intermittent `10000` "System Error" once. `apps/radar/services.py` caches each fetched page for 10 minutes per connection and filters, applies shortcuts and sorts the loaded offers locally. Every listed offer is also cached by item id for 6 hours: the card actions (favourite, create task, schedule post) send only the id, and the server takes the offer — including the image URL it downloads, restricted to Shopee's CDN hosts — from that cache or from the `SavedOffer` snapshot, never from the browser. Task and post creation go through `apps.composer.services.create_draft_post` (a draft aimed at every connected account) and `apps.tasks.services.create_task`. The affiliate's own sales come from `conversionReport` (`ShopeeAffiliateClient.conversions`: times as Int64 strings, pages of 500 followed by `scrollId`, at most the last 90 days); `apps/radar/reports.py` turns them into the analytics page's `DerivedMetric` cards and chart through `apps.analytics.derive.derive`, and `apps.analytics.views.shopee_report` renders them with the analytics templates. Adding a source: a `OpportunityConnection.Source` choice, a client in `providers/`, a connect view and screen in `apps/radar`, and an entry in `sources.SOURCES`. The Radar's Instagram research (`apps/radar/instagram.py`) is not a source: it reads through one of the workspace's Instagram `SocialAccount`s (Facebook Login), with `InstagramProvider.business_discovery` and `hashtag_media`, and stores only the watched handles (`WatchedProfile`).

---

## 6. Publishing

`apps/publisher/engine.py` (`PublishEngine`) runs inside the worker's dedicated publishing lane (§7):

1. `run_publish_cycle` (every 15 s) claims `PlatformPost` rows with `status=scheduled` and `scheduled_at <= now`, and moves them to `publishing`.
2. It dispatches them in parallel. Concurrency is capped by `PUBLISHER_MAX_CONCURRENT_POSTS` and `PUBLISHER_MAX_CONCURRENT_PLATFORM_PUBLISHES`. Each thread holds its own Postgres connection, so these caps are a connection budget (see `.env.example`).
3. Failures retry with exponential backoff. The first comment is posted `PUBLISHER_FIRST_COMMENT_DELAY` seconds (default 120) after go-live.
4. `confirm_pending_publishes` (every 60 s) asks the platform about posts it accepted but is still processing (for example, TikTok transcoding). It also gives up on stale rows (`PUBLISHER_STALE_PUBLISHING_TIMEOUT`, `PUBLISHER_PUBLISH_CONFIRM_TIMEOUT`, `PUBLISHER_UNCONFIRMED_TIMEOUT`). Those end as visible failures and are never retried silently.

A published post leaves its network only when a person asks (`composer:post_delete` with `remote=1`): `delete_from_network` calls the provider's `delete_post` for the row, once per Story when a Story row went out as several. A provider declares what it can delete with the classmethod `can_delete_post(post_format)` and, when deleting is a permission of its own, `delete_post_scope`; `SocialAccount.can_delete_post` adds what the connection was granted. The record is removed only after the network confirmed, so a refusal never leaves a live post without its row.

Status belongs to `PlatformPost`, which is one row per post, account and format (Story, Reels… are their own rows). `Post.status` is an aggregate. Media goes out in one of two ways. For platforms that fetch server-side (Instagram, Threads, Facebook, Pinterest, Google Business, dev.to), the engine sends the absolute URL `APP_URL + asset.file.url`. Otherwise it downloads the file to a temp path and uploads it (`apps/media_library/storage.download_to_path`). There is no pre-publish transcoding step. Files are published as stored.

---

## 7. Background Worker

`python manage.py run_studio_worker` (`apps/common/management/commands/run_studio_worker.py`) runs `StudioWorker` (`apps/common/worker.py`):

- It keeps `django-background-tasks` for storage, row locks and task lifecycle (`bg_runner`), but replaces that library's thread pool with **two lanes of one thread each**. The `publishing` lane runs only `run_publish_cycle` and `confirm_pending_publishes`. The `general` lane runs everything else. A long analytics job therefore can't delay publishing.
- At startup it releases locks left behind by a previous process with the same PID name (PID 1 in a container). This stops a killed task from staying parked until `MAX_RUN_TIME`.
- SIGTERM stops new claims and lets in-flight tasks finish. Locks are never released early. `--duration` makes the worker recycle (general work drains first). Dokploy runs the worker without it.
- `apps/publisher/management/commands/run_publisher.py` is an older standalone publish loop. Production does not use it.

**Recurring tasks** are registered idempotently on `post_migrate`, mostly through `apps/common/background.register_recurring_task`. Registration is skipped if a row with the same `verbose_name` already exists. **Changing an interval constant does not change a task that is already registered.** You have to delete its row first.

| Task (`verbose_name`) | Interval | Registered in |
|------|-----------|---------------|
| `run_publish_cycle` | 15 s | `apps/publisher/apps.py` |
| `confirm_pending_publishes` | 60 s | `apps/publisher/apps.py` |
| `run_inbox_sync_cycle` | 5 min. YouTube accounts also have a per-account floor, `INBOX_YOUTUBE_MIN_POLL_SECONDS` (30 min). | `apps/inbox/apps.py` |
| `sync_all_account_analytics` | hourly | `apps/analytics/apps.py` |
| `schedule_all_health_checks` | 6 h. Validates tokens, refreshes expiring ones, retries webhook subscriptions. | `apps/social_accounts/apps.py` |
| `retry_youtube_revocations` / `enforce_youtube_retention` | hourly / daily | `apps/social_accounts/apps.py` |
| `run_approval_reminders_cycle` | hourly | `apps/approvals/apps.py` |
| `retry_failed_deliveries`, `send_batched_email_digests` | 60 s | `apps/notifications/apps.py` |
| `sweep_pending_uploads` / `run_orphaned_media_sweep` | hourly / daily | `apps/media_library/apps.py` |
| `sweep_idempotency` / `sweep_api_usage` | hourly / daily | `apps/api/apps.py` |
| `purge_automation_logs` | daily (90-day retention) | `apps/automations/apps.py` |
| `expire_courtesies` | daily | `apps/billing/apps.py` |
| `purge_email_counters` | daily | `apps/common/apps.py` |
| `clear_expired_sessions` | daily | `apps/accounts/apps.py` |
| `send_task_due_reminders` | hourly | `apps/tasks/apps.py` |
| `intelligence_reconcile` | 6 h (only relevant when Intelligence is enabled) | `apps/intelligence/apps.py` |

One-off `@background` jobs include media processing (`apps/media_library/tasks.py`), automation runs (`apps/automations/tasks.run_automation_for_message`), first comments and analytics backfills.

`apps/calendar/tasks.generate_recurring_posts` exists and is tested, but no recurring task registers it.

---

## 8. Inbox and Inbound Webhooks

`InboxMessage` (`apps/inbox/models.py`) has types `comment|mention|tag|dm|review` (`tag`: an Instagram post that tagged the account, read from the `/tags` edge, which cannot be answered). No provider produces `review` yet. Messages arrive two ways:

- **Polling:** `InboxSyncEngine` (`apps/inbox/tasks.py`) calls `provider.get_messages()` every 5 minutes. YouTube is rate-limited per account, and a weekly deep sweep (`INBOX_DEEP_SWEEP_SECONDS`) catches replies on old threads. Polling always runs. Webhooks only make delivery faster. A newly connected account starts with an empty inbox: `SocialAccount.inbox_history_cutoff` is set at first connect, the first poll asks only for what came after it, and every poll drops older messages (providers read with an overlap, and the sweeps walk whole threads). "Import history" in the account's menu (`social_accounts:import_inbox_history`) queues `import_inbox_history`, which runs the same uncapped walk as `manage.py backfill_inbox` (`InboxSyncEngine.import_history`) and lifts the floor once it succeeded. A null floor means no filtering, which is also what accounts connected before the floor existed have; those are offered the reverse, "Remove old history" (`clear_inbox_history`), which puts the floor at `connected_at` and deletes the account's older messages with their replies and notes. Both are tracked in `SocialAccount.inbox_history_job` (`running` / `done` / `failed`, with the count or the reason): while it runs the card shows it and polls `social_accounts:account_card`, whose answer carries the result toast; a job running for more than 30 minutes is taken as lost and the action is offered again.
- **Webhooks** (`apps/inbox/webhook_urls.py`, CSRF-exempt, rate-limited):

| Endpoint | Source | Verification |
|----------|--------|--------------|
| `/webhooks/facebook/` | Facebook Pages + Instagram (Facebook Login): feed, mentions, comments, messages, postbacks | GET handshake with `FACEBOOK_WEBHOOK_VERIFY_TOKEN`. POST checks `X-Hub-Signature-256` HMAC against every configured app secret, and events are applied only to accounts whose org's secret signed the body. |
| `/webhooks/instagram_login/` | Instagram Login accounts | Same scheme, with `INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN` |
| `/webhooks/whatsapp/` | WhatsApp Business Accounts (field `messages`): customer messages and delivery statuses | Same scheme, with `WHATSAPP_WEBHOOK_VERIFY_TOKEN` (defaults to the Facebook one) and the `whatsapp` app secret. An event applies only to the connected number named in `metadata.phone_number_id` whose `webhook_target_id` is the entry's WABA. A `failed` status marks the matching `InboxReply` failed with a readable reason. |
| `/webhooks/youtube/` | YouTube PubSubHubbub (Atom XML) | `X-Hub-Signature` HMAC with `YOUTUBE_WEBHOOK_SECRET`. XML is parsed with `safe_xml_fromstring`. |

Handlers process the payload **synchronously** in the request: they find the `SocialAccount`, dedupe on `(social_account, platform_message_id)`, link the message to its `PlatformPost`, run keyword sentiment (`apps/inbox/sentiment.py`), send notifications, and call `apps.automations.services.on_inbox_message_created`. That call only enqueues work and never raises. Meta postbacks (button taps) resume automation flows (§9).

Webhook subscription goes through `provider.subscribe_webhooks`, which is implemented for Facebook, Instagram and Instagram Login. It runs on connect, during health checks, and from `manage.py subscribe_webhooks`. No code in the repo subscribes YouTube channels to the PubSubHubbub hub, so YouTube comments arrive through polling.

DM sending eligibility (Meta's 24-hour window and the optional Human Agent tag, `PLATFORM_*_HUMAN_AGENT_ENABLED`; for WhatsApp the 24-hour customer service window with no extension, after which only approved templates are delivered) is decided in one place, `apps/inbox/reply_policy.py`. The web inbox, REST and MCP all use it.

WhatsApp media arrives as a media id behind an authenticated, short-lived URL; `inbox:message_media` downloads it with the account token (25 MB cap, Meta media hosts only) and serves images, video and audio inline and everything else as a download.

**Live inbox.** The open feed polls `inbox:version` (the workspace's newest `created_at`, backed by the `inbox_msg_ws_created` index) every 4 seconds and refetches the list, with the filters last applied, only when it changes. Webhook deliveries therefore show up within seconds without a reload; polled platforms still depend on the 5-minute cycle.

Threads also calls two endpoints about a person's grant rather than their content (`apps/social_accounts/platform_callbacks.py`): `/webhooks/threads/deauthorize/` when they remove the app, which marks their connections `disconnected` with a reason, and `/webhooks/threads/data-deletion/` when they ask for deletion, which removes the connections with their data (`services.remove_account_data`), records a `DataDeletionRequest` and answers with a confirmation code and the `/data-deletion/?code=…` status link. The Facebook app's data deletion callback is the public page's own address: a POST to `/data-deletion/` (or `/exclusao-de-dados/`) is handed to `facebook_data_deletion`, which removes every Page and Instagram connection granted by the Facebook user the request names. That user is recorded at connect time in `SocialAccount.granted_by_platform_user_id` (read with `debug_token`), since the connection itself is a Page or an Instagram profile; a connection without it is not found until it is reconnected. All of these are a POST with a `signed_request` (HMAC-SHA256 with that app's secret); a connection is touched only when the request is signed with its own organization's secret.

Other inbound webhooks: `/webhooks/resend/` (Svix-signed bounces/suppressions → `EmailSuppression`, `apps/common/webhooks.py`) and `/webhooks/asaas/` (§11).

**Contacts.** Both birth points also call `apps.contacts.services.on_inbox_message_created`, which, like the automations hook, never raises: it runs `record_inbox_sender` after the commit and only logs a failure. A sender is matched on `ContactChannel(workspace, kind, external_id)` (the inbox `sender_handle`, unique per workspace and kind when set), then on the channel's value, so the platform ID from a webhook and the @handle from polling end on one contact. The daily `refresh_contacts` task (`apps/contacts/tasks.py`) covers what the hook missed, capped at `REFRESH_SENDER_CAP` senders per run.

---

## 9. Automations (Comment Replies and Flows)

`apps/automations/` answers comments on Facebook, Instagram and Instagram Login posts (`matching.COMMENT_PLATFORMS`). Google Business review replies are listed as planned only (`channels.py`).

- **Authoring:** a `PostAutomation` stores the canvas graph (`{"nodes", "edges"}`) for one `PlatformPost`. It can be created while the post is still a draft, and it is armed when the publisher confirms the post (`services.arm_for_platform_post`). `flow.py` compiles the graph down into an `AutomationRule`: keywords, post scope, public reply and DM. It never decompiles a rule back into a graph. Staff publish reusable `FlowTemplate` graphs.
- **Matching:** `matching.py` runs rules in priority order, and the first match wins. It performs no writes. `keywords.py` supports `exact`, `contains`, `any_word` and accent-insensitive `_loose` variants (for pt-BR).
- **Execution:** `actions.run_for_message` checks refusals first, then claims idempotency guards (`guards.py`: one reply per sender per post, `max_per_hour`). The guards rely on database constraints and conditional updates. The public reply and the private DM are independent halves. Flows with more steps run in `engine.py`. The walk sends only the private nodes (`send_dm`, `send_card`): `reply_comment` is the public reply, already posted by `actions` before the walk starts, so the walk passes through it. What the first wake delivered is recorded on the claim row (`private_reply_sent_at`, or a note in `detail` when nothing went out), the same record the two-halves path leaves. A `FlowRun` stores progress keyed by `(social_account, sender_key)`, and a button postback resumes the run from a node id. The walk is bounded by a visited set and `MAX_STEPS_PER_WAKE`, and it stops at the first send failure.
- **Audit:** `AutomationLog` rows double as idempotency claims and are purged after 90 days.

---

## 10. Analytics

`apps/analytics/tasks.py` collects account snapshots and post metrics hourly. Two sources are not `SocialAccount`s and are read live instead: Shopee Affiliates (`apps/radar/reports.py`) and Meta Ads (`apps/ads/reports.py`); both build the page's `DerivedMetric` cards and chart with `derive`, and `fmt_metric` takes `currency:<CODE>` for an ad account that does not run in BRL. Each post's sync interval depends on its age (`post_sync_interval`). Failures back off, and a revoked grant queues a health check. YouTube reports are written in the background and read from cache (`youtube_reports.py`). `history_import.py` and `manage.py import_post_history` / `backfill_analytics` import a channel's past posts and metrics.

---

## 11. Billing (Asaas)

`apps/billing/`. **The workspace is the billable unit:** one plan with every feature, priced at `BILLING_WORKSPACE_PRICE_CENTS` per month (default R$ 50,00). Disk is the only metered resource. The base quota is `BILLING_WORKSPACE_BASE_STORAGE_BYTES` (10 GB), and extra GB are sold as `WorkspaceStorageAddon`. Money is always stored as integer cents and disk as integer bytes.

- Models: `WorkspaceSubscription` (`pending|active|past_due|canceled|courtesy`. Only `active` and `courtesy` grant access), `OrganizationPlan`, `Coupon` / `CouponRedemption`, `WorkspaceAddon`, `BillingEvent` (append-only, keyed by gateway event id), `OrganizationBillingProfile` (comped workspaces) and `ProductPrice` / `ProductPriceChange`. Each subscription keeps a snapshot of the price it was sold at.
- Gateway: `apps/billing/gateways/asaas.py` is the only module that knows Asaas's wire format. It is configured by `ASAAS_API_KEY`, `ASAAS_API_BASE` (defaults to the **sandbox**), `ASAAS_WEBHOOK_TOKEN` and `ASAAS_TIMEOUT_SECONDS`. If the key or token is missing, the integration does nothing.
- `ASAAS_API_KEY` is written in the env file without its leading `$` (`aact_...`): Docker Compose and django-environ read `$name` as another variable, so the key would arrive empty. `config/settings/base.py` reads it from `os.environ` and puts the `$` back (`_asaas_api_key`).
- Customer group: every customer created or updated here is sent with `groupName` = `ASAAS_CUSTOMER_GROUP` (default "Connekt Flow"), so this product's payers are grouped apart from the other tools billing through the same Asaas account. `manage.py asaas_group_customers` puts the customers created before into it.
- Webhook: `/webhooks/asaas/` authenticates with the `asaas-access-token` header. Asaas does not sign the body.
- Organization plan (`OrganizationPlan`, `contracting.purchase_plan`): the checkout (`billing:checkout`) sells N brands on one Asaas subscription, priced by `services.quote_plan` (each brand at its position's volume tier, the yearly discount on top). The first charge is paid by card on the spot (`POST /payments/{id}/payWithCreditCard`) and the card token is put on the subscription (`PUT /subscriptions/{id}/creditCard`), so renewals are charged without the customer. The plan covers `brands` workspaces that are neither comped nor paying on a subscription of their own, the ones ticked at checkout first (then the oldest, so a brand created later takes a free one): their `WorkspaceSubscription.plan` points at it and their status mirrors the plan's (`services.sync_plan_coverage`, also run by the webhook), so every access check still reads the workspace row. Card details are passed through and never stored; `checkout_pay` is rate-limited per user and only takes brands of the organization that can be ticked. The card charge has its own 60 s timeout (`CARD_CHARGE_TIMEOUT_SECONDS`). A declined card cancels the Asaas subscription; a transport error leaves the plan pending, and the next checkout checks for a paid charge before charging again.
- Storage enforcement: `apps/billing/storage_usage.py` (`BILLING_STORAGE_QUOTA_ENABLED`) is separate from the older per-org cap in `apps/media_library/quotas.py` (`STORAGE_QUOTA_*`).
- UI: `/workspace/<id>/billing/` for customers and `/billing/staff/` for operators.
- Access: `SubscriptionGateMiddleware` (`apps/billing/middleware.py`, after `RBACMiddleware`) redirects any URL carrying a `workspace_id` to the contracting page when `services.subscription_required()` is on (set on Plans and prices, else `BILLING_REQUIRE_SUBSCRIPTION`) and the workspace is not `active` or `courtesy`. HTMX requests get `204` + `HX-Redirect`. The `billing` URL namespace is exempt.

---

## 12. Intelligence (optional)

`apps/intelligence/` connects Studio to the hosted BrightBean Intelligence SaaS (third-party, run by upstream). It is turned on only when all five of `INTELLIGENCE_INTERNAL_URL`, `INTELLIGENCE_PUBLIC_URL`, `STUDIO_DEPLOYMENT_ID`, `STUDIO_SHARED_SECRET` and `STUDIO_BASE_URL` are set (`INTELLIGENCE_ENABLED`). Its URLs (`/orgs/<id>/intelligence/`, `/intelligence/`) are mounted only in that case. The app is always installed so migrations stay consistent. `services/client.py` has two clients. One makes HMAC-signed calls to `/internal/v1/` (provisioning, billing, eligibility), with nonces and idempotency keys. The other calls the public `/v1/` tools with a per-org `bb_` bearer key. Subscription checkout uses Stripe, run by Intelligence. The boot sequence refuses non-https URLs except loopback.

---

## 13. Agent API, MCP and OAuth Server

- **REST** (`apps/api/api.py`, django-ninja) at `/api/v1/`. Routers: `me`, `accounts`, `posts`, `media`, `analytics`, `inbox`. OpenAPI docs are at `/api/v1/docs`. Every route requires authentication. The README ("API & MCP for Agents") documents the endpoints.
- **API keys** (`apps/api_keys/`). Format: `bb_studio_<random32>_<lookup8>`. Only the lookup prefix and an HMAC-SHA256 of the secret part (peppered from `SECRET_KEY`) are stored. Each key is scoped to one workspace, an allowlist of social accounts, and a list of permissions. Its effective permissions are the intersection of that list with the issuer's current workspace permissions. `ApiKeyAuditLog` records each call and `ApiKeyUsageHourly` keeps hourly counts. `IdempotencyRecord` (`apps/api/models.py`) backs idempotent writes. Rate limits live in `apps/api/limits.py`. Client IPs are taken from `X-Forwarded-For` only for addresses listed in `BB_TRUSTED_PROXIES`.
- **MCP** (`apps/mcp/`) at `/api/v1/mcp`. A hand-written Streamable-HTTP transport (POST only, no SSE) exposes about 21 tools from `handlers.py`: accounts, drafts/scheduling, media upload, analytics, and inbox reply drafts and sending. The `mcp` package is listed in `requirements.txt` but is not imported.
- **OAuth 2.1** (`apps/oauth_server/`, django-oauth-toolkit) lets native MCP clients log in without a pre-shared key. It provides Dynamic Client Registration at `/oauth/register`, authorization code + PKCE with S256 only (`validator.py`), https-only redirect URIs and the single scope `mcp`. Discovery documents are served at `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource[/api/v1/mcp]`. `MCP_PUBLIC_BASE_URL` and `MCP_OAUTH_ISSUER_URL` default to `APP_URL`.

---

## 14. Media Storage

`STORAGE_BACKEND` switches the backend (`base.py`):

```
STORAGE_BACKEND=local  →  FileSystemStorage at MEDIA_ROOT (Dokploy: /app/media, volume media_data)
STORAGE_BACKEND=s3     →  storages.backends.s3boto3.S3Boto3Storage (S3_* env vars)
```

**Serving:**
- `s3`: private ACL and presigned URLs that expire after 1 hour. `SERVE_MEDIA` is forced off. `AWS_S3_MAX_MEMORY_SIZE` (2 MB) must be non-zero, otherwise reads hold whole objects in RAM. Large uploads go straight to the bucket via presigned URLs (`create_pending_upload`, valid for `MEDIA_LIBRARY_PRESIGN_EXPIRE`). `sweep_pending_uploads` cleans up abandoned uploads.
- `local`: `SERVE_MEDIA` (default `true`) mounts `MEDIA_URL` → `MEDIA_ROOT` in `config/urls.py` under every settings module, not just `DEBUG`. The route has no authentication because platforms fetch publish media server-side (§6). `django.views.static.serve` does not support byte ranges, so videos can't be seeked. Switch to `s3` once video volume grows.
- Only the prefixes in `PUBLIC_MEDIA_PREFIXES` (`config/urls.py`) are publicly routed: `media_library/`, `avatars/`, `workspaces/icons/`. This is an allowlist, so any new `upload_to` prefix is private by default. `comment_attachments/` is excluded on purpose (internal comments) and is served by `apps.approvals.views.comment_attachment` behind a membership check.
- Uploads are stored with an extension derived from the sniffed MIME type (`apps/media_library/validators.storage_filename`), not from the client's filename. Media assets go through `media_library.services.create_asset` (the composer and idea uploads included, `composer.views._create_idea_media_asset`); the image fields filled in views (avatar, workspace logo, contact photo, comment attachment, the operator's date and flow-template images) use `validators.image_storage_filename`, which refuses anything whose bytes are not an allowed raster image.

**Processing** (`apps/media_library/tasks.py`, `services.py`): on upload, metadata is extracted and a thumbnail is generated with Pillow or FFmpeg. The image editor and video trim produce `MediaAssetVersion`s. Limits are set in `base.py`: 20 MB per image, 30 MP decoded, 1 GB per video, 5-minute FFmpeg timeout. `MEDIA_LIBRARY_MAX_CONCURRENT_TRANSCODES` is defined but nothing reads it. A daily sweep removes orphaned media (assets no post, idea, task art or template references).

---

## 15. Email

`EMAIL_BACKEND` is always `apps.common.mail.BudgetedEmailBackend`, which wraps `EMAIL_INNER_BACKEND`: SMTP when `EMAIL_BACKEND_TYPE=smtp`, otherwise the console backend. It enforces a global daily cap and per-recipient hourly and daily caps (`EMAIL_*_LIMIT`; a negative value means unlimited, `0` means send nothing). `EMAIL_SENDING_ENABLED=false` is the kill switch. Addresses suppressed through the Resend webhook (`RESEND_WEBHOOK_SECRET`) are skipped. Development removes the caps and prints mail to the console.

**Templates and sending.** Every email is a pair of templates: `<prefix>.html` extending `templates/emails/base.html` (the logo, a centred 600 px card that goes full width on a phone, the footer) and `<prefix>.txt` extending `templates/emails/base.txt`. Buttons come from `templates/emails/_button.html`, which also prints the URL as text for clients that block buttons. Mail clients ignore CSS variables and most `<style>`, so the layout is tables with inline styles and literal hex. `apps.common.emails.build_email(subject=…, template_prefix=…, context=…, to=…, headers=…)` renders both parts with `email_context()` (brand name, `APP_URL`, absolute logo URL, legal links, `LEGAL_*` sender identity, year) and returns the message; the caller sends it with `apps.common.mail.send_or_raise`. allauth's mail (password reset, confirmation, unknown or existing account, security notices with `ACCOUNT_EMAIL_NOTIFICATIONS`, social login connected) goes through `AccountAdapter.render_mail`, which merges the same context; its HTML lives in `templates/account/email/` and `templates/socialaccount/email/`, and `account/email/base_message.txt` puts the same footer under allauth's own text bodies. The welcome email is `account/email/welcome`, sent on `user_signed_up`. The password form in the account settings sends allauth's `password_changed` notice itself, since it does not go through allauth.

**Footer.** Every email says why the recipient got it (`{% block reason %}`), that it is automatic and that the product never asks for a password, the support address (`LEGAL_CONTACT_EMAIL`), the privacy and terms pages, and the sender: "Connekt Flow é uma marca do grupo `LEGAL_COMPANY_NAME`", then `LEGAL_COMPANY_DOCUMENT` (CNPJ), `LEGAL_COMPANY_SERVICE_AREA`, `LEGAL_COMPANY_ADDRESS` (one line per `|`-separated part). `LEGAL_COMPANY_PHONE` reaches the templates as `company_phone`, but the footer doesn't show it, and it has no copyright line. The defaults in `config/settings/base.py` are Seven Click's; a key set but empty hides its line. Notification mail also gets the preferences link and the one-click unsubscribe link, the same token as its `List-Unsubscribe` header (`apps.notifications.unsubscribe.notification_email_parts`).

---

## 16. Internationalization

`LANGUAGE_CODE` defaults to `pt-br`. `LANGUAGES` contains `pt-br` and `en-us`. `LocaleMiddleware` runs after the session, and the sidebar language picker posts to `/i18n/setlang/`. Translations live in `locale/pt_BR/LC_MESSAGES/`. The `.mo` files are gitignored and compiled by `compilemessages` in the `Dockerfile` (which installs `gettext`). If they are missing, the interface silently falls back to English. The translation workflow and vocabulary are in the `translation` skill (`.claude/skills/translation/SKILL.md`).

---

## 17. Security

| Concern | Implementation |
|---------|----------------|
| Passwords | `BCryptSHA256PasswordHasher` (PBKDF2 fallback), min length 8 |
| Sessions | DB-backed, HTTP-only, SameSite=Lax, 14-day sliding window. Secure cookies and HSTS in production (`production.py`). |
| Login | django-allauth: email + Google (`GOOGLE_AUTH_*`). `SIGNUP_MODE=open\|invite\|closed`, `CUSTOM_SIGNUP_URL`. |
| 2FA | Not implemented. `User` has encrypted `totp_*` fields, but no flow uses them. |
| Magic links | `MagicLinkToken`: `secrets.token_urlsafe(48)`, stored as is (not hashed), 30-day expiry |
| API keys | HMAC-SHA256 of the secret, lookup prefix, scoped per workspace (§13) |
| Encryption at rest | AES-256-GCM model fields (`apps/common/encryption.py`). The key is derived from `SECRET_KEY` + `ENCRYPTION_KEY_SALT` via HKDF. There is no key-rotation command. Changing either value makes stored tokens unreadable. |
| Data isolation | Explicit `for_org` / `for_workspace` plus membership checks (§3) |
| CSRF / CSP | Django CSRF middleware. `django-csp` with script nonces. Frames are allowed only from `youtube-nocookie.com` (`CSP_FRAME_SRC`, the banners' help videos). CSP is enforced in development too (`CSP_REPORT_ONLY=true` in `.env` turns it into reports). |
| Rate limiting | `django-ratelimit` on webhooks, invites, onboarding and social connects. `AuthRateLimitMiddleware` on auth. Its own limiter for the API. Disabled when `DEBUG`. |
| Audit | `ApiKeyAuditLog`, `BillingEvent`, `AutomationLog`, impersonation sessions. There is no general audit log. |
| Account deletion | Self-service in account settings, refused while the user is the sole owner of an org. No data-export feature. |
| Webhooks | Signature or token verification per source (§8, §11). Legal pages are public (required by Meta App Review): `/exclusao-de-dados/` (and `/data-deletion/`) is served here and receives Meta's signed deletion callback; `/privacidade/` and `/termos/` (and `/privacy/`, `/terms/`) redirect to the public site, `connektflow.com.br/privacidade/` and `/termos/`, where the texts live (`connektflow-site`). |

---

## 18. Deployment

**Production: Dokploy only**, using `docker-compose.dokploy.yml` (a standalone file, not combined with the dev files):

```
migrate:  one-shot `manage.py migrate --noinput`
app:      gunicorn config.wsgi:application --workers 1 --threads 4 (expose 8000, /health/ healthcheck)
worker:   manage.py run_studio_worker
volumes:  media_data (mounted at /app/media in app and worker)
networks: dokploy-network (external), where the database and Traefik are
```

Dokploy's Traefik terminates TLS and proxies to `app:8000`. No host port is published and there is no second proxy. Env vars come from Dokploy's Environment tab. `config.settings.production` trusts `X-Forwarded-Proto` and exempts `/health/` from the SSL redirect. Gunicorn uses one worker because of memory (about 200 MB per warmed worker). `--max-requests` is left off on purpose (see the `Dockerfile` comment). `MALLOC_ARENA_MAX=2` is set on app and worker.

The image (`Dockerfile`, `python:3.12-slim`) installs FFmpeg, gettext and Node 20, builds Tailwind, compiles messages and runs `collectstatic`.

---

## 19. Backup and Restore

There is no backup management command. What needs backing up:

- **PostgreSQL**: all application data, including the job queue. Schedule backups on the database in Dokploy, or run `pg_dump` against `DATABASE_URL`.
- **Media**: the `media_data` volume when `STORAGE_BACKEND=local`. With S3, media lives in the bucket.
- **Secrets**: keep `SECRET_KEY` and `ENCRYPTION_KEY_SALT` with the backup. Without them, encrypted tokens and credentials can't be decrypted (§17).

To restore: load the dump into an empty database, restore the media volume, and deploy. The `migrate` service applies any newer migrations.

---

## 20. Configuration

`.env.example` is the reference for deployable settings. Groups:

- **Core:** `SECRET_KEY`, `ENCRYPTION_KEY_SALT`, `DEBUG`, `ALLOWED_HOSTS`, `APP_URL`, `LANGUAGE_CODE`, `SIGNUP_MODE`, `CUSTOM_SIGNUP_URL`, `DATABASE_URL`
- **Storage:** `STORAGE_BACKEND`, `MEDIA_ROOT`, `SERVE_MEDIA`, `S3_*`, `AWS_S3_MAX_MEMORY_SIZE`
- **Email:** `EMAIL_BACKEND_TYPE`, `EMAIL_HOST*`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL`, `LEGAL_COMPANY_NAME`, `LEGAL_COMPANY_DOCUMENT`, `LEGAL_COMPANY_ADDRESS`, `LEGAL_COMPANY_PHONE`, `LEGAL_COMPANY_SERVICE_AREA`, `LEGAL_CONTACT_EMAIL`
- **Platform apps:** `PLATFORM_FACEBOOK_*`, `PLATFORM_INSTAGRAM_*`, `PLATFORM_THREADS_*`, `PLATFORM_LINKEDIN_{PERSONAL,COMPANY}_*`, `PLATFORM_TIKTOK_*`, `PLATFORM_GOOGLE_*`, `PLATFORM_PINTEREST_*`, `YOUTUBE_COMPLIANCE_ENFORCEMENT`, `UNSPLASH_ACCESS_KEY`
- **Publisher:** `PUBLISHER_*` timeouts and concurrency
- **Webhooks:** `FACEBOOK_WEBHOOK_VERIFY_TOKEN`, `INSTAGRAM_LOGIN_WEBHOOK_VERIFY_TOKEN`, `YOUTUBE_WEBHOOK_SECRET`
- **Billing:** `BILLING_WORKSPACE_PRICE_CENTS`, `BILLING_ANNUAL_DISCOUNT_PERCENT`, `BILLING_VOLUME_DISCOUNTS`, `BILLING_TRIAL_DAYS`, `BILLING_REQUIRE_SUBSCRIPTION` (starting values only: Plans and prices overrides each once saved), `ASAAS_API_KEY`, `ASAAS_API_BASE`, `ASAAS_WEBHOOK_TOKEN`
- **Optional:** `INTELLIGENCE_*` / `STUDIO_*`, `MCP_PUBLIC_BASE_URL`, `MCP_OAUTH_ISSUER_URL`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`

These are read in `config/settings/base.py` but missing from `.env.example`: `SENTRY_DSN`, `REDIS_URL`, `BB_TRUSTED_PROXIES`, `RESEND_WEBHOOK_SECRET`, `EMAIL_SENDING_ENABLED`, `EMAIL_*_LIMIT`, `INVITE_*`, `ASAAS_TIMEOUT_SECONDS`, `BILLING_WORKSPACE_BASE_STORAGE_BYTES`, `BILLING_STORAGE_*`, `STORAGE_QUOTA_ENABLED`, `MAX_ACCOUNTS_PER_PLATFORM`, `INBOX_YOUTUBE_MIN_POLL_SECONDS`, `INBOX_DEEP_SWEEP_SECONDS`, `PUBLISHER_FIRST_COMMENT_*`.

---

## 21. Development and CI

- **Docker:** `docker compose up` merges `docker-compose.yml` with `docker-compose.override.yml`. You get `migrate`, `app` (runserver on :8000, source mounted), `worker`, `postgres` (:5432) and a `tailwind` watcher, all on `config.settings.development`.
- **Without Docker:** see README, "Fully Local Development". It uses `DATABASE_URL=sqlite:///db.sqlite3`, `npm install` + `npm run start` in `theme/static_src`, then `runserver` and `run_studio_worker` in separate terminals.
- **Development settings:** `ALLOWED_HOSTS=*`, console email with no caps (or Mailpit at `http://localhost:8025` when `DEV_SMTP_HOST` is set, as `docker-compose.override.yml` does), enforced CSP (report-only with `CSP_REPORT_ONLY=true`), `CSRF_TRUSTED_ORIGINS` for tunnels (ngrok), debug toolbar if installed.
- **CI** (`.github/workflows/ci.yml`): ruff check + format → mypy (`apps/ config/ providers/ tests/`) → pytest against Postgres (after `makemigrations --check`) → Docker image build → gitleaks. CI does not deploy anything. Deploys are triggered in Dokploy.
