"""When the composer's autosave makes a new draft, and when it does not."""

from apps.composer.models import Post
from apps.composer.tests.test_authorization import ComposerAuthorizationBase


class AutosaveNewDraftTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)

    def _autosave(self, **fields):
        return self.client.post(self._url("autosave"), {"title": "", "caption": "", **fields})

    def test_ticking_channels_alone_leaves_no_draft(self):
        self._autosave(selected_accounts=str(self.account.id))

        assert not Post.objects.filter(workspace=self.ws).exists()

    def test_text_makes_the_draft_with_its_channels(self):
        self._autosave(caption="Launch day", selected_accounts=str(self.account.id))

        post = Post.objects.get(workspace=self.ws)
        assert list(post.platform_posts.values_list("social_account_id", flat=True)) == [self.account.id]
