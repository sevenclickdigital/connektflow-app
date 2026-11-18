"""Security regression tests for the May-2026 audit.

Covers V2 (hex-color CSS injection), V6 (queue category IDOR), and V7
(missing role check on custom calendar events).
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import CustomCalendarEvent, Queue, QueueEntry
from apps.composer.models import ContentCategory, PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _make_user(email):
    user = User.objects.create_user(
        email=email,
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    # The accounts post_save signal auto-provisions a default Organization +
    # Workspace + OrgMembership for every new User. Tests that want to attach
    # the user to a specific org must start from a clean slate, otherwise the
    # RBAC middleware (which does OrgMembership.objects.filter(...).first())
    # may pick the auto-org instead of the test org.
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization

    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class CalendarEventColorValidationTests(TestCase):
    """V2 + V7: event create/edit require role and reject non-hex colors."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.owner = _make_user("owner@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, workspace_role="owner")
        self.viewer = _make_user("viewer@example.com")
        OrgMembership.objects.create(user=self.viewer, organization=self.org, org_role="member")
        WorkspaceMembership.objects.create(user=self.viewer, workspace=self.workspace, workspace_role="viewer")

    def test_viewer_cannot_create_event(self):
        self.client.force_login(self.viewer)
        url = reverse("calendar:event_create", kwargs={"workspace_id": self.workspace.id})
        response = self.client.post(
            url,
            data={
                "title": "Launch",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
                "color": "#FF0000",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CustomCalendarEvent.objects.exists())

    def test_owner_with_bad_color_is_rejected(self):
        self.client.force_login(self.owner)
        url = reverse("calendar:event_create", kwargs={"workspace_id": self.workspace.id})
        response = self.client.post(
            url,
            data={
                "title": "Launch",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
                "color": "red;background-image:url('//evil')",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomCalendarEvent.objects.exists())

    def test_owner_with_good_color_succeeds(self):
        self.client.force_login(self.owner)
        url = reverse("calendar:event_create", kwargs={"workspace_id": self.workspace.id})
        response = self.client.post(
            url,
            data={
                "title": "Launch",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
                "color": "#FF0000",
            },
        )
        self.assertLess(response.status_code, 400)
        self.assertEqual(CustomCalendarEvent.objects.count(), 1)

    def test_event_edit_rejects_bad_color(self):
        event = CustomCalendarEvent.objects.create(
            workspace=self.workspace,
            title="Existing",
            start_date="2026-06-01",
            end_date="2026-06-01",
            color="#3B82F6",
            created_by=self.owner,
        )
        self.client.force_login(self.owner)
        url = reverse(
            "calendar:event_edit",
            kwargs={"workspace_id": self.workspace.id, "event_id": event.id},
        )
        response = self.client.post(url, data={"color": "red;url(x)"})
        self.assertEqual(response.status_code, 400)
        event.refresh_from_db()
        self.assertEqual(event.color, "#3B82F6")


class QueueCreateCategoryIdorTests(TestCase):
    """V6: queue_create must validate category_id against the request workspace."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.ws_a = Workspace.objects.create(organization=self.org, name="WS-A")
        self.ws_b = Workspace.objects.create(organization=self.org, name="WS-B")
        self.user = _make_user("user@example.com")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role="owner")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.ws_a, workspace_role="owner")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.ws_b, workspace_role="owner")
        self.account = SocialAccount.objects.create(
            workspace=self.ws_a,
            platform="bluesky",
            account_name="bsky",
            account_platform_id="did:plc:test",
            connection_status="connected",
        )
        self.foreign_category = ContentCategory.objects.create(workspace=self.ws_b, name="Foreign", color="#000000")

    def test_foreign_category_id_returns_404(self):
        self.client.force_login(self.user)
        url = reverse("calendar:queue_create", kwargs={"workspace_id": self.ws_a.id})
        response = self.client.post(
            url,
            data={
                "name": "Test Queue",
                "social_account_id": str(self.account.id),
                "category_id": str(self.foreign_category.id),
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Queue.objects.filter(workspace=self.ws_a).exists())

    def test_no_category_creates_queue(self):
        self.client.force_login(self.user)
        url = reverse("calendar:queue_create", kwargs={"workspace_id": self.ws_a.id})
        response = self.client.post(
            url,
            data={
                "name": "Test Queue",
                "social_account_id": str(self.account.id),
            },
        )
        self.assertLess(response.status_code, 400)
        self.assertTrue(Queue.objects.filter(workspace=self.ws_a, name="Test Queue").exists())


class QueueMutationPermissionTests(TestCase):
    """Queue writes take a write permission; changing a queued post takes rights over that post."""

    def setUp(self):
        self.org = Organization.objects.create(name="Queue Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Queue WS")
        self.users = {}
        for role in ("owner", "contributor", "client", "viewer"):
            user = _make_user(f"{role}@example.com")
            OrgMembership.objects.create(
                user=user, organization=self.org, org_role="owner" if role == "owner" else "member"
            )
            WorkspaceMembership.objects.create(user=user, workspace=self.workspace, workspace_role=role)
            self.users[role] = user
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="bluesky",
            account_name="bsky",
            account_platform_id="did:plc:queue",
            connection_status="connected",
        )
        self.queue = Queue.objects.create(workspace=self.workspace, name="Q", social_account=self.account)
        now = timezone.now()
        self.first_at = now + timedelta(days=1)
        self.second_at = now + timedelta(days=2)
        self.owner_entry, self.owner_pp = self._queue_post(self.users["owner"], self.first_at, 0)
        self.own_entry, self.own_pp = self._queue_post(self.users["contributor"], self.second_at, 1)

    def _queue_post(self, author, slot, position):
        post = Post.objects.create(workspace=self.workspace, author=author, caption="queued")
        pp = PlatformPost.objects.create(
            post=post, social_account=self.account, status=PlatformPost.Status.SCHEDULED, scheduled_at=slot
        )
        entry = QueueEntry.objects.create(queue=self.queue, post=post, position=position, assigned_slot_datetime=slot)
        return entry, pp

    def _url(self, name, **kwargs):
        return reverse(f"calendar:{name}", kwargs={"workspace_id": self.workspace.id, **kwargs})

    def _assert_schedule_untouched(self):
        self.owner_pp.refresh_from_db()
        self.own_pp.refresh_from_db()
        self.assertEqual(self.owner_pp.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.owner_pp.scheduled_at, self.first_at)
        self.assertEqual(self.own_pp.scheduled_at, self.second_at)

    def test_read_only_roles_cannot_create_or_delete_a_queue(self):
        for role in ("viewer", "client", "contributor"):
            with self.subTest(role=role):
                self.client.force_login(self.users[role])
                created = self.client.post(
                    self._url("queue_create"), data={"name": "Mine", "social_account_id": str(self.account.id)}
                )
                deleted = self.client.post(self._url("queue_delete", queue_id=self.queue.id))
                self.assertEqual(created.status_code, 403)
                self.assertEqual(deleted.status_code, 403)
        self.assertEqual(list(Queue.objects.filter(workspace=self.workspace)), [self.queue])

    def test_read_only_roles_cannot_change_another_authors_entry(self):
        entry_kwargs = {"queue_id": self.queue.id, "entry_id": self.owner_entry.id}
        for role in ("viewer", "client", "contributor"):
            with self.subTest(role=role):
                self.client.force_login(self.users[role])
                removed = self.client.post(self._url("queue_entry_remove", **entry_kwargs), HTTP_HX_REQUEST="true")
                reslotted = self.client.post(self._url("queue_entry_reslot", **entry_kwargs), HTTP_HX_REQUEST="true")
                reordered = self.client.post(
                    self._url("queue_reorder", queue_id=self.queue.id),
                    data={"entry_ids": f"{self.own_entry.id},{self.owner_entry.id}"},
                )
                self.assertEqual(removed.status_code, 403)
                self.assertEqual(reslotted.status_code, 403)
                self.assertEqual(reordered.status_code, 403)
        self.assertTrue(QueueEntry.objects.filter(id=self.owner_entry.id).exists())
        self._assert_schedule_untouched()

    def test_author_removes_their_own_entry(self):
        self.client.force_login(self.users["contributor"])
        response = self.client.post(
            self._url("queue_entry_remove", queue_id=self.queue.id, entry_id=self.own_entry.id),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(QueueEntry.objects.filter(id=self.own_entry.id).exists())
        self.own_pp.refresh_from_db()
        self.assertEqual(self.own_pp.status, PlatformPost.Status.DRAFT)

    def test_owner_reorders_removes_and_deletes(self):
        self.client.force_login(self.users["owner"])
        reordered = self.client.post(
            self._url("queue_reorder", queue_id=self.queue.id),
            data={"entry_ids": f"{self.own_entry.id},{self.owner_entry.id}"},
        )
        self.assertEqual(reordered.status_code, 200)
        self.owner_pp.refresh_from_db()
        self.own_pp.refresh_from_db()
        self.assertEqual(self.own_pp.scheduled_at, self.first_at)
        self.assertEqual(self.owner_pp.scheduled_at, self.second_at)

        removed = self.client.post(
            self._url("queue_entry_remove", queue_id=self.queue.id, entry_id=self.own_entry.id),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(QueueEntry.objects.filter(id=self.own_entry.id).exists())

        deleted = self.client.post(self._url("queue_delete", queue_id=self.queue.id))
        self.assertLess(deleted.status_code, 400)
        self.assertFalse(Queue.objects.filter(id=self.queue.id).exists())

    def test_queue_pages_hide_the_controls_the_member_cannot_use(self):
        self.client.force_login(self.users["viewer"])
        listing = self.client.get(self._url("queue_list"))
        detail = self.client.get(self._url("queue_detail", queue_id=self.queue.id))
        self.assertNotContains(listing, self._url("queue_create"))
        self.assertNotContains(listing, self._url("queue_delete", queue_id=self.queue.id))
        self.assertNotContains(
            detail, self._url("queue_entry_remove", queue_id=self.queue.id, entry_id=self.owner_entry.id)
        )
