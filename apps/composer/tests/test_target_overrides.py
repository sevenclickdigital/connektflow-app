"""A post's per-channel customization comes back when the post is reopened.

The Customize cards post their fields on every save, and a blank one clears
the customization, so the composer must open them filled with what was saved.
"""

from apps.composer.models import PlatformPost
from apps.composer.tests.test_authorization import ComposerAuthorizationBase


class TargetOverridesTests(ComposerAuthorizationBase):
    def test_the_saved_customization_reaches_the_composer(self):
        post, row = self._post(self.manager)
        PlatformPost.objects.filter(pk=row.pk).update(
            platform_specific_title="Own title", platform_specific_caption="Own caption"
        )
        self.client.force_login(self.manager)

        response = self.client.get(self._url("compose_edit", post_id=post.id))

        self.assertEqual(
            response.context["target_overrides"],
            {f"{self.account.id}:{row.format}": {"title": "Own title", "caption": "Own caption"}},
        )

    def test_a_post_without_customization_sends_none(self):
        post, _row = self._post(self.manager)
        self.client.force_login(self.manager)

        response = self.client.get(self._url("compose_edit", post_id=post.id))

        self.assertEqual(response.context["target_overrides"], {})
