"""Workspace-scoped billing URLs.

Mounted under ``/workspace/<uuid:workspace_id>/billing/`` beside the
composer, calendar and media library — billing is a property of the
workspace, which is the billable unit.

The staff console lives in ``staff_urls`` instead, because it has no
workspace (or organization) in its path by design: it lists every
account so an operator can comp one. See ``views._is_billing_staff``.
"""

from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("", views.workspace_billing, name="workspace_billing"),
    path("subscribe/", views.subscribe_modal, name="subscribe_modal"),
    path("subscribe/confirm/", views.subscribe_confirm, name="subscribe_confirm"),
    path("trial/", views.start_trial, name="start_trial"),
    path("pay/", views.pay_invoice, name="pay_invoice"),
    path("checkout/", views.checkout, name="checkout"),
    path("checkout/quote/", views.checkout_quote, name="checkout_quote"),
    path("checkout/pay/", views.checkout_pay, name="checkout_pay"),
    path("checkout/add-brands/", views.checkout_add_brands, name="checkout_add_brands"),
    path("storage/quote/", views.storage_quote, name="storage_quote"),
    path("storage/", views.contract_storage, name="contract_storage"),
]
