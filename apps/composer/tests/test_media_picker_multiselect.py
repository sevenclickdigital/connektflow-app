"""The media picker selects several assets at once and inserts them in order.

Picking used to be one asset per click, each a round trip that closed the
modal, with the post's media order left to however the attachments happened to
land. These pin the replacement: one request carries the whole selection, the
click order becomes the carousel order, and the picker runs the library's own
search/filter/sort/paginate rather than the first 50 assets.

Asserts on HTTP status, database state and context only. No assertion on any
rendered sentence: the project defaults to pt-BR and copy is not a contract.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Post, PostMedia
from apps.media_library.models import MediaAsset, MediaFolder
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class MediaPickerTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.client.force_login(self.user)

    def _asset(self, filename="a.png", media_type=MediaAsset.MediaType.IMAGE, **kwargs):
        asset = MediaAsset(
            organization=self.org,
            workspace=self.workspace,
            filename=filename,
            media_type=media_type,
            file_size=1,
            processing_status=MediaAsset.ProcessingStatus.COMPLETED,
            **kwargs,
        )
        # The card template renders ``asset.file.url`` for an image, which
        # raises on an asset with no file. Only the path is needed — nothing
        # here reads the bytes.
        asset.file = f"media_library/2026/01/{filename}"
        asset.save()
        return asset

    def _post(self):
        return Post.objects.create(workspace=self.workspace, caption="hi")

    def _picker(self, **params):
        return self.client.get(
            reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}),
            params,
        )


class AttachBatchTests(MediaPickerTestCase):
    def test_every_selected_asset_is_attached_in_one_request(self):
        post = self._post()
        assets = [self._asset(f"{i}.png") for i in range(4)]

        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(a.id) for a in assets]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.media_attachments.count(), 4)

    def test_attachment_order_follows_the_order_they_were_picked(self):
        """Position, not arrival: the first thumbnail clicked leads the carousel."""
        post = self._post()
        first, second, third = (self._asset(f"{i}.png") for i in range(3))

        # Deliberately not the creation order — this is what the DB would
        # hand back if the view trusted its own ordering.
        picked = [third, first, second]
        self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(a.id) for a in picked]},
        )

        ordered = list(
            PostMedia.objects.filter(post=post).order_by("position").values_list("media_asset_id", flat=True)
        )
        self.assertEqual(ordered, [a.id for a in picked])

    def test_selection_without_a_post_is_held_as_pending_media(self):
        assets = [self._asset(f"{i}.png") for i in range(3)]

        response = self.client.post(
            reverse("composer:attach_media_batch", kwargs={"workspace_id": self.workspace.id}),
            {"media_asset_ids": [str(a.id) for a in assets]},
        )

        self.assertEqual(response.status_code, 200)
        pending = self.client.session[f"pending_media_{self.workspace.id}"]
        self.assertEqual(pending, [str(a.id) for a in assets])

    def test_an_empty_selection_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(post.media_attachments.count(), 0)

    def test_another_workspace_asset_is_dropped_from_the_selection(self):
        """The id filter is the workspace's own visibility, not the POST body."""
        post = self._post()
        mine = self._asset("mine.png")
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        theirs = MediaAsset(
            organization=self.org,
            workspace=other_ws,
            filename="theirs.png",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=1,
        )
        theirs.file = "media_library/2026/01/theirs.png"
        theirs.save()

        self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": [str(mine.id), str(theirs.id)]},
        )

        attached = list(PostMedia.objects.filter(post=post).values_list("media_asset_id", flat=True))
        self.assertEqual(attached, [mine.id])

    def test_a_malformed_id_does_not_take_down_the_whole_insert(self):
        """A non-UUID reaching the ORM raises ValidationError, i.e. a 500."""
        post = self._post()
        good = self._asset("good.png")

        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["not-a-uuid", str(good.id)]},
        )

        self.assertEqual(response.status_code, 200)
        attached = list(PostMedia.objects.filter(post=post).values_list("media_asset_id", flat=True))
        self.assertEqual(attached, [good.id])

    def test_a_selection_of_only_malformed_ids_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["not-a-uuid", ""]},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_selection_of_only_unknown_ids_is_refused(self):
        post = self._post()
        response = self.client.post(
            reverse(
                "composer:attach_media_batch_post",
                kwargs={"workspace_id": self.workspace.id, "post_id": post.id},
            ),
            {"media_asset_ids": ["0f2b7a44-0000-4000-8000-000000000000"]},
        )
        self.assertEqual(response.status_code, 404)


class PickerListingTests(MediaPickerTestCase):
    def test_the_picker_paginates_instead_of_stopping_at_fifty(self):
        """The old picker sliced [:50]; anything past it could not be reached."""
        for i in range(60):
            self._asset(f"{i:03}.png")

        first = self._picker()
        self.assertEqual(first.status_code, 200)
        page = first.context["page"]
        self.assertTrue(page.has_next())

        second = self._picker(page=2)
        self.assertEqual(second.context["page"].number, 2)
        seen = page.paginator.count
        self.assertEqual(seen, 60)

    def test_search_narrows_the_listing(self):
        # Searched by a whole word of the name: Postgres tokenizes
        # "christmas tree.png" as "christmas" + "tree.png", so a bare
        # "christmas" matches here but would not match "christmas.png",
        # which indexes as one token.
        self._asset("christmas tree.png")
        self._asset("summer sale.png")

        response = self._picker(q="christmas")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["christmas tree.png"])

    def test_the_type_filter_narrows_the_listing(self):
        self._asset("a.png", media_type=MediaAsset.MediaType.IMAGE)
        self._asset("b.mp4", media_type=MediaAsset.MediaType.VIDEO)

        response = self._picker(type=MediaAsset.MediaType.VIDEO)

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["b.mp4"])

    def test_the_folder_filter_narrows_the_listing(self):
        folder = MediaFolder.objects.create(organization=self.org, workspace=self.workspace, name="Natal")
        self._asset("in-folder.png", folder=folder)
        self._asset("loose.png")

        response = self._picker(folder=str(folder.id))

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["in-folder.png"])

    def test_favorites_narrow_the_listing(self):
        self._asset("starred.png", is_starred=True)
        self._asset("plain.png")

        response = self._picker(starred="1")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["starred.png"])

    def test_sorting_by_name_reorders_the_listing(self):
        self._asset("zebra.png")
        self._asset("apple.png")

        response = self._picker(sort="name")

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["apple.png", "zebra.png"])

    def test_a_grid_targeted_request_returns_the_grid_alone(self):
        """Searching swaps the grid; it must not swap the modal's own chrome."""
        self._asset("a.png")

        response = self.client.get(
            reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="picker-asset-grid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([t.name for t in response.templates][0], "media_library/_asset_grid.html")

    def test_opening_the_picker_returns_the_whole_modal_body(self):
        self._asset("a.png")

        response = self._picker()

        self.assertEqual([t.name for t in response.templates][0], "composer/partials/media_picker.html")
        self.assertTrue(response.context["picker_mode"])
        self.assertIn("folders", response.context)

    def test_another_workspace_media_is_not_listed(self):
        self._asset("mine.png")
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        theirs = MediaAsset(
            organization=self.org,
            workspace=other_ws,
            filename="theirs.png",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=1,
        )
        theirs.file = "media_library/2026/01/theirs.png"
        theirs.save()

        response = self._picker()

        names = [a.filename for a in response.context["page"].object_list]
        self.assertEqual(names, ["mine.png"])


class PickedBeforeThePostExistedTests(MediaPickerTestCase):
    """The picker opened on an unsaved post inserts into the session.

    When an autosave creates the post while the picker is open, those picks
    stay in the session; the saved post has to claim them or it publishes
    without its media.
    """

    def _save(self, post, **data):
        return self.client.post(
            reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id}),
            {"caption": "hi", "action": "save_draft", **data},
        )

    def _hold_pending(self, *assets):
        session = self.client.session
        session[f"pending_media_{self.workspace.id}"] = [str(asset.id) for asset in assets]
        session.save()

    def test_a_saved_post_takes_the_pending_media_its_form_lists(self):
        post = self._post()
        existing = self._asset("existing.png")
        PostMedia.objects.create(post=post, media_asset=existing, position=0)
        picked = self._asset("picked.png")
        self._hold_pending(picked)

        self._save(post, pending_media_ids=[str(picked.id)])

        ordered = list(post.media_attachments.order_by("position").values_list("media_asset_id", flat=True))
        self.assertEqual(ordered, [existing.id, picked.id])
        self.assertNotIn(f"pending_media_{self.workspace.id}", self.client.session)

    def test_a_saved_post_leaves_pending_media_its_form_does_not_list(self):
        post = self._post()
        other_tab = self._asset("other-tab.png")
        self._hold_pending(other_tab)

        self._save(post)

        self.assertEqual(post.media_attachments.count(), 0)
        self.assertEqual(self.client.session[f"pending_media_{self.workspace.id}"], [str(other_tab.id)])


class PhotoLimitTests(MediaPickerTestCase):
    def test_publishing_more_photos_than_facebook_takes_is_refused(self):
        from apps.social_accounts.models import SocialAccount

        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="page-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post = self._post()
        for position in range(16):
            PostMedia.objects.create(post=post, media_asset=self._asset(f"{position}.png"), position=position)

        response = self.client.post(
            reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id}),
            {
                "caption": "hi",
                "action": "publish_now",
                "selected_accounts": str(account.id),
                "selected_targets": f"{account.id}:default",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("media", response.json()["errors"])
        self.assertFalse(post.platform_posts.exists())


class IdeaMediaPickerTests(MediaPickerTestCase):
    """The same window on the idea board hands the selection back to the page."""

    def _url(self, workspace=None):
        return reverse("composer:idea_media_picker", kwargs={"workspace_id": (workspace or self.workspace).id})

    def test_the_window_and_its_grid_stay_on_the_idea_picker(self):
        asset = self._asset()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["picker_target"], "idea")
        page = response.content.decode()
        # Each card says what the idea's window needs to show it once picked.
        self.assertIn(f'data-asset-url="{asset.file.url}"', page)
        # Uploads go to the idea's endpoint and nothing is attached to a post.
        self.assertIn(reverse("composer:idea_upload_media", kwargs={"workspace_id": self.workspace.id}), page)
        self.assertNotIn(reverse("composer:attach_media_batch", kwargs={"workspace_id": self.workspace.id}), page)
        # Paginating or refreshing the grid must not fall back to the composer's picker.
        grid = self.client.get(self._url(), headers={"HX-Target": "picker-asset-grid"}).content.decode()
        self.assertIn(self._url(), grid)
        self.assertNotIn(reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}), grid)

    def test_an_upload_lands_in_the_folder_open_in_the_window_but_never_anothers(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        folder = MediaFolder.objects.create(
            organization=self.workspace.organization, workspace=self.workspace, name="Tasks"
        )
        other = Workspace.objects.create(organization=Organization.objects.create(name="Other Org"), name="Other")
        foreign = MediaFolder.objects.create(organization=other.organization, workspace=other, name="Theirs")
        url = reverse("composer:idea_upload_media", kwargs={"workspace_id": self.workspace.id})
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

        kept = self.client.post(
            url, {"file": SimpleUploadedFile("a.png", png, "image/png"), "folder_id": str(folder.id)}
        )
        dropped = self.client.post(
            url, {"file": SimpleUploadedFile("b.png", png, "image/png"), "folder_id": str(foreign.id)}
        )

        self.assertEqual(MediaAsset.objects.get(id=kept.json()["asset_id"]).folder, folder)
        self.assertIsNone(MediaAsset.objects.get(id=dropped.json()["asset_id"]).folder)

    def test_another_brand_cannot_open_it(self):
        other_org = Organization.objects.create(name="Other Org")
        other = Workspace.objects.create(organization=other_org, name="Other")

        response = self.client.get(self._url(other))

        self.assertIn(response.status_code, (403, 404))


class IdeaCropTests(MediaPickerTestCase):
    """Cropping an image of an idea makes a copy and describes it to the page."""

    def _url(self, asset, workspace=None):
        return reverse(
            "composer:idea_crop_media",
            kwargs={"workspace_id": (workspace or self.workspace).id, "asset_id": asset.id},
        )

    def test_a_missing_area_is_refused_and_nothing_is_created(self):
        asset = self._asset()

        response = self.client.post(self._url(asset), {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_a_video_is_refused(self):
        asset = self._asset("a.mp4", media_type=MediaAsset.MediaType.VIDEO)

        response = self.client.post(self._url(asset), {"x": 0, "y": 0, "width": 10, "height": 10})

        self.assertEqual(response.status_code, 400)

    def test_an_image_of_another_brand_is_not_found(self):
        other_org = Organization.objects.create(name="Other Org")
        other = Workspace.objects.create(organization=other_org, name="Other")
        foreign = MediaAsset(
            organization=other_org,
            workspace=other,
            filename="b.png",
            media_type=MediaAsset.MediaType.IMAGE,
            file_size=1,
            processing_status=MediaAsset.ProcessingStatus.COMPLETED,
        )
        foreign.file = "media_library/2026/01/b.png"
        foreign.save()

        response = self.client.post(self._url(foreign), {"x": 0, "y": 0, "width": 10, "height": 10})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(MediaAsset.objects.count(), 1)

    def test_a_viewer_cannot_crop(self):
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.workspace).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        OrgMembership.objects.filter(user=self.user).update(org_role=OrgMembership.OrgRole.MEMBER)
        asset = self._asset()

        response = self.client.post(self._url(asset), {"x": 0, "y": 0, "width": 10, "height": 10})

        self.assertEqual(response.status_code, 403)
