"""Query parameters of the two calendar pages never become JavaScript.

``mode``, ``view`` and ``tab`` are written into the page's Alpine state; the
organisation calendar's filters used to be written into its ``@change``
handlers. HTML escaping does not protect an Alpine expression — the browser
decodes the attribute before Alpine evaluates it — so the tests read the
attributes decoded, as a browser does.
"""

from html.parser import HTMLParser

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

BREAKOUT = "'+alert(document.domain)+'"


class _ExpressionAttributes(HTMLParser):
    """Collects the decoded value of every Alpine attribute (``x-*``, ``@*``, ``:*``)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(value or "" for name, value in attrs if name.startswith(("x-", "@", ":")))


def _expressions(response):
    parser = _ExpressionAttributes()
    parser.feed(response.content.decode())
    return parser.values


class CalendarQueryParamTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        # Creating a user provisions an organization; the test's own must be the only one.
        own = list(OrgMembership.objects.filter(user=self.user).values_list("organization_id", flat=True))
        WorkspaceMembership.objects.filter(user=self.user).delete()
        OrgMembership.objects.filter(user=self.user).delete()
        Organization.objects.filter(id__in=own).delete()

        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role="owner")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, workspace_role="owner")
        # A connected channel, so the page draws the calendar and its mode toggle.
        SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-1",
            account_name="ig",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.client.force_login(self.user)
        self.workspace_calendar = reverse("calendar:calendar", kwargs={"workspace_id": self.workspace.id})
        self.org_calendar = reverse("organizations:cross_workspace_calendar")

    def test_an_unknown_mode_falls_back_and_is_not_evaluated(self):
        response = self.client.get(self.workspace_calendar, {"mode": BREAKOUT, "view": BREAKOUT, "tab": BREAKOUT})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["mode"], "calendar")
        self.assertEqual(response.context["view_type"], "month")
        self.assertEqual(response.context["active_tab"], "queue")
        expressions = _expressions(response)
        self.assertIn("{ activeMode: 'calendar' }", expressions)
        self.assertFalse([value for value in expressions if "alert" in value and "document.domain" in value])

    def test_the_known_modes_and_views_still_work(self):
        response = self.client.get(self.workspace_calendar, {"mode": "calendar", "view": "list"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.context["mode"], response.context["view_type"]), ("calendar", "list"))

        response = self.client.get(self.workspace_calendar, {"mode": "board"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("{ activeMode: 'board' }", _expressions(response))

        response = self.client.get(self.workspace_calendar, {"mode": "list", "tab": "drafts"})
        self.assertEqual(response.context["active_tab"], "drafts")

    def test_the_organisation_calendar_keeps_request_values_out_of_its_handlers(self):
        payload = "';alert(document.domain);'"
        response = self.client.get(self.org_calendar, {"view": payload, "status": payload, "tag": payload})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view_type"], "month")
        expressions = _expressions(response)
        self.assertFalse([value for value in expressions if "alert" in value])
        for name in ("status", "channel", "tag"):
            self.assertIn(f"setFilter('{name}', $event.target.value)", expressions)

    def test_the_organisation_calendar_still_switches_view(self):
        response = self.client.get(self.org_calendar, {"view": "week"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view_type"], "week")
