"""Which Publish entry the sidebar marks on the composer's pages."""

import re

from django.test import override_settings
from django.urls import reverse

from apps.composer.tests.test_account_scope import AccountScopeTestsBase


def _active_subnav(html):
    return re.findall(r'class="sidebar-subnav-item active">([^<]+)</a>', html)


# Asserts on menu labels; pin English so it tests the markup, not the catalogue.
@override_settings(LANGUAGE_CODE="en-us")
class SidebarMenuTests(AccountScopeTestsBase):
    def test_editing_a_post_marks_the_calendar(self):
        url = reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id})

        html = self.client.get(url).content.decode()

        self.assertEqual(_active_subnav(html), ["Calendar"])

    def test_a_new_post_marks_the_calendar(self):
        html = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})).content.decode()

        self.assertEqual(_active_subnav(html), ["Calendar"])

    def test_the_idea_hub_marks_itself(self):
        url = reverse("composer:create_landing", kwargs={"workspace_id": self.workspace.id})

        html = self.client.get(url).content.decode()

        self.assertEqual(_active_subnav(html), ["Idea hub"])
