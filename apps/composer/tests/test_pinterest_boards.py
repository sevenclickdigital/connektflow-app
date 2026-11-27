"""The composer's Pinterest board list: what the board picker gets back."""

from unittest import mock

from django.urls import reverse

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
        self.assertEqual(response.json(), {"boards": [{"id": "b1", "name": "Ideas"}]})

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


class PinterestBoardCreateTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.pinterest = SocialAccount.objects.create(
            workspace=self.ws,
            platform="pinterest",
            account_platform_id="pin-1",
            account_name="Pins",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.url = self._url("pinterest_board_create", account_id=self.pinterest.id)

    def test_creates_the_board_and_answers_it(self):
        self.client.force_login(self.manager)
        with mock.patch.object(
            PinterestProvider, "create_board", return_value={"id": "b9", "name": "Launch"}
        ) as create:
            response = self.client.post(self.url, {"name": "  Launch "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "b9", "name": "Launch"})
        self.assertEqual(create.call_args.args[1], "Launch")

    def test_a_refusal_says_what_pinterest_said(self):
        self.client.force_login(self.manager)
        error = ProviderError("400", platform="pinterest", user_message="Board name taken.")
        with mock.patch.object(PinterestProvider, "create_board", side_effect=error):
            response = self.client.post(self.url, {"name": "Launch"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "Board name taken."})

    def test_a_blank_name_is_refused_before_calling_pinterest(self):
        self.client.force_login(self.manager)
        with mock.patch.object(PinterestProvider, "create_board") as create:
            response = self.client.post(self.url, {"name": " "})
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_member_who_cannot_create_posts_is_refused(self):
        viewer = self._member("viewer")
        self.client.force_login(viewer)
        with mock.patch.object(PinterestProvider, "create_board") as create:
            response = self.client.post(self.url, {"name": "Launch"})
        self.assertEqual(response.status_code, 403)
        create.assert_not_called()


class PinterestProviderBoardsTests(ComposerAuthorizationBase):
    def test_every_page_of_boards_is_read(self):
        provider = PinterestProvider({"client_id": "id", "client_secret": "secret"})
        pages = [
            {"items": [{"id": "b1"}], "bookmark": "next"},
            {"items": [{"id": "b2"}], "bookmark": None},
        ]
        responses = [mock.Mock(json=mock.Mock(return_value=page)) for page in pages]
        with mock.patch.object(PinterestProvider, "_request", side_effect=responses) as request:
            boards = provider.get_boards("token")
        self.assertEqual([b["id"] for b in boards], ["b1", "b2"])
        self.assertEqual(request.call_args_list[1].kwargs["params"]["bookmark"], "next")


class PinterestPinCheckTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        from apps.composer.models import PlatformPost, Post

        self.pinterest = SocialAccount.objects.create(
            workspace=self.ws,
            platform="pinterest",
            account_platform_id="pin-1",
            account_name="Pins",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post = Post.objects.create(workspace=self.ws, author=self.manager, caption="pin")
        self.row = PlatformPost.objects.create(
            post=post, social_account=self.pinterest, status="published", platform_post_id="1129559150354629778"
        )
        self.url = self._url("pinterest_pin", platform_post_id=self.row.id)
        self.client.force_login(self.manager)

    def test_shows_what_pinterest_has(self):
        pin = {
            "id": "1129559150354629778",
            "title": "Oi",
            "description": "Prazer, sou o MOTI!",
            "board_id": "b1",
            "created_at": "2026-11-27T19:13:25",
            "media": {"images": {"600x": {"url": "https://i.pinimg.com/564x/a.jpg"}}},
        }
        with (
            mock.patch.object(PinterestProvider, "get_pin", return_value=pin) as get_pin,
            mock.patch.object(PinterestProvider, "get_board", return_value={"name": "Connekt Flow News"}) as get_board,
        ):
            response = self.client.get(self.url)
        assert response.status_code == 200
        assert get_pin.call_args.args[1] == "1129559150354629778"
        assert get_board.call_args.args[1] == "b1"
        body = response.content.decode()
        assert "1129559150354629778" in body
        assert "Prazer, sou o MOTI!" in body
        assert "https://i.pinimg.com/564x/a.jpg" in body
        assert "Connekt Flow News" in body
        assert "2026-11-27T19:13:25" not in body

    def test_the_board_id_stands_in_when_its_name_cannot_be_read(self):
        pin = {"id": "1129559150354629778", "board_id": "b1"}
        error = ProviderError("500", platform="pinterest")
        with (
            mock.patch.object(PinterestProvider, "get_pin", return_value=pin),
            mock.patch.object(PinterestProvider, "get_board", side_effect=error),
        ):
            response = self.client.get(self.url)
        assert response.status_code == 200
        assert "b1" in response.content.decode()

    def test_a_refusal_shows_the_reason(self):
        error = ProviderError("404", platform="pinterest", user_message="Pin not found.")
        with mock.patch.object(PinterestProvider, "get_pin", side_effect=error):
            response = self.client.get(self.url)
        assert response.status_code == 200
        assert "Pin not found." in response.content.decode()

    def test_another_workspaces_pin_is_not_found(self):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other")
        url = reverse("composer:pinterest_pin", kwargs={"workspace_id": other.id, "platform_post_id": self.row.id})
        with mock.patch.object(PinterestProvider, "get_pin") as get_pin:
            response = self.client.get(url)
        assert response.status_code in (403, 404)
        get_pin.assert_not_called()


def test_pin_metrics_ask_for_the_last_90_days_up_to_today():
    """Pinterest refuses a start date older than 90 days or an end date in the future."""
    from datetime import date

    from providers.pinterest import PinterestProvider

    assert PinterestProvider.metrics_window(date(2026, 11, 27)) == {
        "start_date": "2026-08-29",
        "end_date": "2026-11-27",
    }


class TestPinHistory:
    """``list_existing_posts``: the account's own pins become history; repins and ad-only pins do not."""

    def _provider(self, pages):
        import httpx

        from providers.pinterest import PinterestProvider

        provider = PinterestProvider({"app_id": "a", "app_secret": "s"})
        calls = []

        def answer(method, url, **kwargs):
            calls.append(kwargs.get("params"))
            return httpx.Response(200, json=pages[len(calls) - 1], request=httpx.Request(method, url))

        return provider, calls, answer

    def test_own_pins_are_read_newest_first_and_others_are_left_out(self):
        pages = [
            {
                "items": [
                    {
                        "id": "11",
                        "title": "Novidade",
                        "description": "Conheça",
                        "created_at": "2026-11-27T19:13:25",
                        "media": {
                            "media_type": "image",
                            "images": {"600x": {"url": "https://i.pinimg.com/600x/a.jpg"}},
                        },
                    },
                    {"id": "12", "is_removable": True, "media": {}},
                    {"id": "13", "is_owner": False, "media": {}},
                ],
                "bookmark": "b2",
            },
            {
                "items": [
                    {"id": "14", "media": {"media_type": "video", "cover_image_url": "https://i.pinimg.com/v.jpg"}}
                ],
                "bookmark": None,
            },
        ]
        provider, calls, answer = self._provider(pages)
        with mock.patch.object(provider, "_request", side_effect=answer):
            posts = provider.list_existing_posts("tok", limit=50)
        assert [p.platform_post_id for p in posts] == ["11", "14"]
        first = posts[0]
        assert first.extra["title"] == "Novidade"
        assert first.caption == "Conheça"
        assert first.thumbnail_url == "https://i.pinimg.com/600x/a.jpg"
        assert first.permalink == "https://www.pinterest.com/pin/11/"
        assert first.published_at.tzinfo is not None
        assert posts[1].media_kind == "video"
        assert posts[1].thumbnail_url == "https://i.pinimg.com/v.jpg"
        assert calls[0]["pin_filter"] == "exclude_repins"
        assert calls[1]["bookmark"] == "b2"

    def test_a_bookmark_chain_that_never_ends_stops(self):
        from providers.pinterest import MAX_HISTORY_PAGES

        pages = [{"items": [], "bookmark": "again"}] * (MAX_HISTORY_PAGES + 5)
        provider, calls, answer = self._provider(pages)
        with mock.patch.object(provider, "_request", side_effect=answer):
            assert provider.list_existing_posts("tok", limit=50) == []
        assert len(calls) == MAX_HISTORY_PAGES
