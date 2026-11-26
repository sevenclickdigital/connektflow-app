"""Saving a hashtag from the composer's "#" panel (``composer:tag_create``)."""

from apps.composer.models import Tag
from apps.composer.tests.test_authorization import ComposerAuthorizationBase


class SavedHashtagTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)

    def test_a_typed_hashtag_is_saved_without_its_sigil(self):
        response = self.client.post(self._url("tag_create"), {"name": "#natal2026", "kind": "hashtag"})

        assert response.status_code == 200
        assert response.json()["name"] == "natal2026"
        tag = Tag.objects.get(workspace=self.ws, name="natal2026")
        assert tag.kind == Tag.Kind.HASHTAG

    def test_a_name_longer_than_the_column_is_cut(self):
        response = self.client.post(self._url("tag_create"), {"name": "a" * 150, "kind": "hashtag"})

        assert response.status_code == 200
        assert Tag.objects.get(workspace=self.ws).name == "a" * 100

    def test_a_sigil_alone_is_not_a_name(self):
        response = self.client.post(self._url("tag_create"), {"name": "#", "kind": "hashtag"})

        assert response.status_code == 400
        assert not Tag.objects.exists()
