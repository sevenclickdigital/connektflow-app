"""The composer's Pinterest board list: what the board picker gets back."""

from unittest import mock

from apps.composer.tests.test_authorization import ComposerAuthorizationBase
from apps.social_accounts.models import SocialAccount
from providers.exceptions import ProviderError
from providers.pinterest import PinterestProvider


class PinterestBoardsTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.pinterest = SocialAccount.objects.create(
            workspace=self.ws,
            platform="pinterest",
            account_platform_id="pin-1",
            account_name="Pins",
            account_handle="seven.click",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.client.force_login(self.manager)
        self.url = self._url("pinterest_boards", account_id=self.pinterest.id)

    def test_lists_the_boards(self):
        boards = [{"id": "b1", "name": "Ideas", "privacy": "PUBLIC"}]
        with mock.patch.object(PinterestProvider, "get_boards", return_value=boards):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"boards": [{"id": "b1", "name": "Ideas"}], "create_url": "https://www.pinterest.com/seven.click/"},
        )

    def test_a_refusal_says_what_pinterest_said(self):
        error = ProviderError("401", platform="pinterest", user_message="Your app is in trial.")
        with mock.patch.object(PinterestProvider, "get_boards", side_effect=error):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "Your app is in trial."})

    def test_a_failure_without_a_sentence_is_still_reported(self):
        with mock.patch.object(PinterestProvider, "get_boards", side_effect=ProviderError("timeout")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.json()["error"])

    def test_an_account_of_another_platform_is_not_found(self):
        url = self._url("pinterest_boards", account_id=self.account.id)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
