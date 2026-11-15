"""Account-centre billing: the overview of every brand the user pays for.

Mounted at ``/organizations/billing/`` beside the other account-centre pages;
each brand's own billing page stays under ``/workspace/<id>/billing/``.
"""

from django.urls import path

from . import views

app_name = "billing_account"

urlpatterns = [
    path("", views.account_billing, name="overview"),
    path("payer/", views.update_payer, name="update_payer"),
]
