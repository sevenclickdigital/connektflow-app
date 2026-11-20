"""The inbox page: filters, sort, entry details and the actions that redraw them."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def member_client(client, inbox_workspace, org_owner, user):
    WorkspaceMembership.objects.create(
        user=user, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client.force_login(user)
    return client


def _feed(workspace):
    return reverse("inbox:feed", kwargs={"workspace_id": workspace.id})


def _message(workspace, account, pid, *, hours_ago, related_post=None, **fields):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id=pid,
        sender_name=pid,
        body=f"body {pid}",
        received_at=timezone.now() - timedelta(hours=hours_ago),
        related_post=related_post,
        **fields,
    )


def _platform_post(workspace, account, fmt):
    post = Post.objects.create(workspace=workspace, caption=f"{fmt} caption")
    return PlatformPost.objects.create(
        post=post, social_account=account, format=fmt, status=PlatformPost.Status.PUBLISHED
    )


@pytest.mark.django_db
class TestFeedFilters:
    def test_the_type_buttons_count_unread_entries_under_the_channel(
        self, member_client, inbox_workspace, inbox_account
    ):
        instagram = SocialAccount.objects.create(
            workspace=inbox_workspace, platform="instagram", account_platform_id="ig-1", account_name="IG"
        )
        _message(inbox_workspace, inbox_account, "dm-1", hours_ago=1, message_type="dm")
        _message(inbox_workspace, inbox_account, "dm-read", hours_ago=1, message_type="dm", status="open")
        _message(inbox_workspace, inbox_account, "c-1", hours_ago=1, message_type="comment")
        _message(inbox_workspace, instagram, "c-ig", hours_ago=1, message_type="comment")
        url = _feed(inbox_workspace)

        response = member_client.get(url, {"platform": "facebook", "type": "dm"}, HTTP_HX_REQUEST="true")

        # The counts ignore the type picked, so every button keeps its number.
        assert response.context["type_counts"] == {"dm": 1, "comment": 1, "mention": 0}
        assert response.context["total"] == 2
        assert 'id="inbox-type-counts"' in response.content.decode()

    def test_oldest_first(self, member_client, inbox_workspace, inbox_account):
        newer = _message(inbox_workspace, inbox_account, "newer", hours_ago=1)
        older = _message(inbox_workspace, inbox_account, "older", hours_ago=5)
        url = _feed(inbox_workspace)

        response = member_client.get(url, {"sort": "oldest"}, HTTP_HX_REQUEST="true")

        assert [m.id for m in response.context["inbox_messages"]] == [older.id, newer.id]

    def test_one_platform_button_per_connected_platform(self, member_client, inbox_workspace, inbox_account):
        SocialAccount.objects.create(
            workspace=inbox_workspace,
            platform="facebook",
            account_platform_id="page-2",
            account_name="Second page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        url = _feed(inbox_workspace)

        response = member_client.get(url)

        assert response.content.decode().count("filterPlatform('facebook')") == 1

    def test_another_workspaces_entries_are_not_listed(
        self, member_client, inbox_workspace, inbox_account, organization
    ):
        other = Workspace.objects.create(name="Other", organization=organization)
        other_account = SocialAccount.objects.create(
            workspace=other, platform="facebook", account_platform_id="page-x", account_name="X"
        )
        _message(other, other_account, "foreign", hours_ago=1)
        url = _feed(inbox_workspace)

        response = member_client.get(url, HTTP_HX_REQUEST="true")

        assert response.context["total"] == 0

    def test_the_menu_has_no_whatsapp_shortcut(self, member_client, inbox_workspace):
        url = _feed(inbox_workspace)

        page = member_client.get(url).content.decode()

        assert "?platform=whatsapp" not in page


@pytest.mark.django_db
class TestEntryActions:
    def test_changing_the_sentiment_redraws_the_entry(self, member_client, inbox_workspace, inbox_message):
        url = reverse(
            "inbox:change_sentiment", kwargs={"workspace_id": inbox_workspace.id, "message_id": inbox_message.id}
        )

        response = member_client.post(url, {"sentiment": "negative"})

        assert response.status_code == 200
        assert 'id="inbox-details-title"' in response.content.decode()
        inbox_message.refresh_from_db()
        assert inbox_message.sentiment == "negative"
        assert inbox_message.sentiment_source == InboxMessage.SentimentSource.MANUAL

    def test_bulk_action_answers_without_a_list(self, member_client, inbox_workspace, inbox_message):
        url = reverse("inbox:bulk_action", kwargs={"workspace_id": inbox_workspace.id})

        response = member_client.post(url, {"message_ids": str(inbox_message.id), "action": "resolve"})

        assert response.status_code == 204
        inbox_message.refresh_from_db()
        assert inbox_message.status == InboxMessage.Status.RESOLVED

    def test_the_entry_shows_its_post(self, member_client, inbox_workspace, inbox_account):
        message = _message(
            inbox_workspace,
            inbox_account,
            "on-reel",
            hours_ago=1,
            related_post=_platform_post(inbox_workspace, inbox_account, "reel"),
            extra={"post_permalink_url": "https://www.instagram.com/reel/abc/"},
        )
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": message.id})

        body = member_client.get(url, HTTP_HX_REQUEST="true").content.decode()

        assert "reel caption" in body
        assert 'href="https://www.instagram.com/reel/abc/"' in body


@pytest.mark.django_db
class TestConversations:
    def test_one_entry_per_person_and_kind(self, member_client, inbox_workspace, inbox_account):
        old = _message(inbox_workspace, inbox_account, "dm-a", hours_ago=5, message_type="dm", sender_handle="ana")
        new = _message(inbox_workspace, inbox_account, "dm-b", hours_ago=1, message_type="dm", sender_handle="ana")
        comment = _message(
            inbox_workspace, inbox_account, "c-a", hours_ago=2, message_type="comment", sender_handle="ana"
        )

        response = member_client.get(_feed(inbox_workspace), HTTP_HX_REQUEST="true")

        entries = response.context["inbox_messages"]
        assert [m.id for m in entries] == [new.id, comment.id]
        assert entries[0].thread_count == 2
        assert entries[0].thread_unread == 2
        assert old.id not in [m.id for m in entries]

    def test_comments_on_different_posts_stay_apart(self, member_client, inbox_workspace, inbox_account):
        for pid, post in (("c1", "p1"), ("c2", "p2")):
            _message(
                inbox_workspace,
                inbox_account,
                pid,
                hours_ago=1,
                message_type="comment",
                sender_handle="ana",
                extra={"post_id": post},
            )

        response = member_client.get(_feed(inbox_workspace), HTTP_HX_REQUEST="true")

        assert response.context["total"] == 2

    def test_opening_the_entry_shows_and_reads_the_whole_conversation(
        self, member_client, inbox_workspace, inbox_account
    ):
        old = _message(inbox_workspace, inbox_account, "dm-a", hours_ago=5, message_type="dm", sender_handle="ana")
        new = _message(inbox_workspace, inbox_account, "dm-b", hours_ago=1, message_type="dm", sender_handle="ana")
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": new.id})

        body = member_client.get(url, HTTP_HX_REQUEST="true").content.decode()

        assert "body dm-a" in body
        old.refresh_from_db()
        assert old.status == InboxMessage.Status.OPEN

    def test_resolving_resolves_the_conversation(self, member_client, inbox_workspace, inbox_account):
        old = _message(inbox_workspace, inbox_account, "dm-a", hours_ago=5, message_type="dm", sender_handle="ana")
        new = _message(inbox_workspace, inbox_account, "dm-b", hours_ago=1, message_type="dm", sender_handle="ana")
        other = _message(inbox_workspace, inbox_account, "dm-c", hours_ago=1, message_type="dm", sender_handle="bob")
        url = reverse("inbox:change_status", kwargs={"workspace_id": inbox_workspace.id, "message_id": new.id})

        member_client.post(url, {"status": "resolved"})

        statuses = dict(InboxMessage.objects.values_list("id", "status"))
        assert statuses[old.id] == statuses[new.id] == "resolved"
        assert statuses[other.id] == "unread"


@pytest.mark.django_db
class TestPostPreview:
    def _comment(self, workspace, account):
        return _message(
            workspace,
            account,
            "c-1",
            hours_ago=1,
            message_type="comment",
            extra={"post_id": "123_456", "stored_post_id": "456"},
        )

    def test_a_post_published_elsewhere_is_read_from_the_platform_once(
        self, member_client, inbox_workspace, inbox_account
    ):
        from unittest import mock

        from django.core.cache import cache

        cache.clear()
        message = self._comment(inbox_workspace, inbox_account)
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": message.id})
        preview = {
            "caption": "Catálogo Online",
            "image_url": "https://cdn/x.jpg",
            "url": "https://fb/p",
            "product_type": "feed",
        }

        with mock.patch("providers.facebook.FacebookProvider.get_post_preview", return_value=preview) as read:
            first = member_client.get(url, HTTP_HX_REQUEST="true").content.decode()
            member_client.get(url, HTTP_HX_REQUEST="true")

        assert "Catálogo Online" in first
        assert 'src="https://cdn/x.jpg"' in first
        read.assert_called_once_with("tok", "123_456")

    def test_a_refused_read_says_so(self, member_client, inbox_workspace, inbox_account):
        from unittest import mock

        from django.core.cache import cache

        from providers.exceptions import APIError

        cache.clear()
        message = self._comment(inbox_workspace, inbox_account)
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": message.id})

        with mock.patch(
            "providers.facebook.FacebookProvider.get_post_preview", side_effect=APIError("nope", platform="facebook")
        ):
            response = member_client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert response.context["post_preview"]["error"] is True


@pytest.mark.django_db
class TestChat:
    def test_the_conversation_reads_as_a_chat_in_order(self, member_client, inbox_workspace, inbox_account, user):
        from apps.inbox.models import InboxReply, InternalNote

        first = _message(inbox_workspace, inbox_account, "dm-a", hours_ago=3, message_type="dm", sender_handle="ana")
        InboxReply.objects.create(
            inbox_message=first,
            author=user,
            body="our answer",
            status=InboxReply.Status.SENT,
            sent_at=timezone.now() - timedelta(hours=2),
        )
        last = _message(inbox_workspace, inbox_account, "dm-b", hours_ago=1, message_type="dm", sender_handle="ana")
        InternalNote.objects.create(inbox_message=last, author=user, body="team only")
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": last.id})

        response = member_client.get(url, HTTP_HX_REQUEST="true")

        chat = response.context["chat"]
        assert [item["kind"] for item in chat] == ["in", "out", "in", "note"]
        assert chat[0]["day"]  # the first bubble opens its day
        assert chat[1]["tail"] and not chat[3].get("day")
        body = response.content.decode()
        assert body.index("body dm-a") < body.index("our answer") < body.index("body dm-b") < body.index("team only")


@pytest.mark.django_db
class TestOrigin:
    def test_each_entry_shows_its_posts_origin(self, member_client, inbox_workspace, inbox_account):
        instagram = SocialAccount.objects.create(
            workspace=inbox_workspace, platform="instagram", account_platform_id="ig-1", account_name="IG"
        )
        _message(
            inbox_workspace,
            instagram,
            "on-reel",
            hours_ago=1,
            message_type="comment",
            extra={"post_id": "m1", "stored_post_id": "m1", "post_origin": "reel"},
        )

        body = member_client.get(_feed(inbox_workspace), HTTP_HX_REQUEST="true").content.decode()

        assert "Reels" in body

    def test_reading_the_post_keeps_its_origin_on_the_comments(self, member_client, inbox_workspace):
        from unittest import mock

        from django.core.cache import cache

        cache.clear()
        instagram = SocialAccount.objects.create(
            workspace=inbox_workspace,
            platform="instagram",
            account_platform_id="ig-1",
            account_name="IG",
            oauth_access_token="tok",
        )
        opened = _message(
            inbox_workspace,
            instagram,
            "c1",
            hours_ago=1,
            message_type="comment",
            sender_handle="ana",
            extra={"post_id": "m1", "stored_post_id": "m1"},
        )
        sibling = _message(
            inbox_workspace,
            instagram,
            "c2",
            hours_ago=2,
            message_type="comment",
            sender_handle="bia",
            extra={"post_id": "m1", "stored_post_id": "m1"},
        )
        preview = {"caption": "x", "image_url": "", "url": "https://ig/p", "product_type": "story"}
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": opened.id})

        with mock.patch("providers.instagram.InstagramProvider.get_post_preview", return_value=preview):
            member_client.get(url, HTTP_HX_REQUEST="true")

        sibling.refresh_from_db()
        assert sibling.extra["post_origin"] == "story"


@pytest.mark.django_db
class TestTagPost:
    def test_a_tag_shows_the_post_it_came_from_without_a_platform_read(self, member_client, inbox_workspace):
        from unittest import mock

        instagram = SocialAccount.objects.create(
            workspace=inbox_workspace, platform="instagram", account_platform_id="ig-1", account_name="IG"
        )
        tag = _message(
            inbox_workspace,
            instagram,
            "tag_1",
            hours_ago=1,
            message_type="tag",
            sender_handle="webdelivery.app",
            extra={
                "media_id": "1",
                "permalink": "https://www.instagram.com/p/abc/",
                "post_image": "https://cdn/tag.jpg",
                "post_origin": "feed",
            },
        )
        url = reverse("inbox:message_detail", kwargs={"workspace_id": inbox_workspace.id, "message_id": tag.id})

        with mock.patch("providers.instagram.InstagramProvider.get_post_preview") as read:
            body = member_client.get(url, HTTP_HX_REQUEST="true").content.decode()

        read.assert_not_called()
        assert 'src="https://cdn/tag.jpg"' in body
        assert 'href="https://www.instagram.com/p/abc/"' in body


@pytest.mark.django_db
class TestGroupByPost:
    def _comments(self, workspace, account):
        for pid, handle, post, hours in (
            ("c1", "ana", "p1", 3),
            ("c2", "bia", "p1", 2),
            ("c3", "ana", "p2", 1),
        ):
            _message(
                workspace,
                account,
                pid,
                hours_ago=hours,
                message_type="comment",
                sender_handle=handle,
                extra={"post_id": post},
            )

    def _post_url(self, workspace, account):
        return reverse("inbox:post_comments", kwargs={"workspace_id": workspace.id, "account_id": account.id})

    def test_comments_come_one_entry_per_post_and_the_choice_is_remembered(
        self, member_client, inbox_workspace, inbox_account
    ):
        self._comments(inbox_workspace, inbox_account)
        dm = _message(inbox_workspace, inbox_account, "dm", hours_ago=4, message_type="dm", sender_handle="ana")

        grouped = member_client.get(_feed(inbox_workspace), {"group": "post"}, HTTP_HX_REQUEST="true")
        again = member_client.get(_feed(inbox_workspace), HTTP_HX_REQUEST="true")
        by_person = member_client.get(_feed(inbox_workspace), {"group": "person"}, HTTP_HX_REQUEST="true")

        rows = {(m.thread_post, m.thread_count, m.is_post_group) for m in grouped.context["inbox_messages"]}
        assert rows == {("p1", 2, True), ("p2", 1, True), ("", 1, False)}
        assert dm.id in {m.id for m in grouped.context["inbox_messages"]}
        assert again.context["group"] == "post"
        assert by_person.context["total"] == 4

    def test_the_post_lists_its_comments_and_reads_them(self, member_client, inbox_workspace, inbox_account):
        from unittest import mock

        from django.core.cache import cache

        cache.clear()
        self._comments(inbox_workspace, inbox_account)
        url = self._post_url(inbox_workspace, inbox_account)

        with mock.patch("providers.facebook.FacebookProvider.get_post_preview", return_value={"caption": "Launch"}):
            response = member_client.get(url, {"post": "p1"}, HTTP_HX_REQUEST="true")

        assert [c.platform_message_id for c in response.context["comments"]] == ["c2", "c1"]
        assert "Launch" in response.content.decode()
        assert set(
            InboxMessage.objects.filter(platform_message_id__in=["c1", "c2"]).values_list("status", flat=True)
        ) == {InboxMessage.Status.OPEN}
        assert InboxMessage.objects.get(platform_message_id="c3").status == InboxMessage.Status.UNREAD

    def test_unanswered_leaves_out_the_comments_with_a_sent_reply(self, member_client, inbox_workspace, inbox_account):
        from apps.inbox.models import InboxReply

        self._comments(inbox_workspace, inbox_account)
        answered = InboxMessage.objects.get(platform_message_id="c2")
        InboxReply.objects.create(inbox_message=answered, body="Thanks", status=InboxReply.Status.SENT)
        url = self._post_url(inbox_workspace, inbox_account)

        rows = member_client.get(url, {"post": "p1", "show": "unanswered", "rows": "1"}).context["comments"]

        assert [c.platform_message_id for c in rows] == ["c1"]

    def test_another_workspaces_post_is_not_found(self, member_client, inbox_workspace, organization):
        other = Workspace.objects.create(organization=organization, name="Other")
        account = SocialAccount.objects.create(
            workspace=other, platform="facebook", account_platform_id="pg-x", account_name="X"
        )
        _message(other, account, "c-x", hours_ago=1, message_type="comment", extra={"post_id": "p1"})

        url = reverse("inbox:post_comments", kwargs={"workspace_id": inbox_workspace.id, "account_id": account.id})

        response = member_client.get(url, {"post": "p1"})

        assert response.status_code == 404
