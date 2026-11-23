"""The administrative area: every screen of the team, under one prefix.

Mounted at ``/staff/`` by ``config/urls.py``. The prefix is the boundary
between the team and the product: ``OperatorAreaMiddleware`` keeps a team
account inside it, every view under it carries ``staff_required`` or
``superadmin_required`` (``apps/accounts/decorators.py``), and
``tests/test_staff_routes_are_staff_only.py`` walks whatever is mounted
here. A new administrative screen joins the area by being included below.

None of these paths carries a ``workspace_id`` or an organization: the
screens are about every account, so there is no membership a check could
key on. Each app keeps its routes in its own ``staff_urls`` module and
namespace.
"""

from django.urls import include, path

urlpatterns = [
    # Customer service and billing; the bare path is Customers, the area's home.
    path("", include("apps.billing.staff_urls")),
    path("affiliates/", include("apps.affiliates.staff_urls")),
    # The product's own content.
    path("flow-templates/", include("apps.automations.staff_urls")),
    path("content-templates/", include("apps.composer.staff_urls")),
    path("special-dates/", include("apps.calendar.staff_urls")),
    path("market-hashtags/", include("apps.radar.staff_urls")),
    path("help-videos/", include("apps.onboarding.staff_urls")),
    # System.
    path("platforms/", include("apps.social_accounts.staff_urls")),
    path("team/", include("apps.accounts.staff_urls")),
]
