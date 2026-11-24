from django.urls import path

from . import views

app_name = "affiliates"

urlpatterns = [
    path("", views.panel, name="panel"),
    path("payout/", views.request_payout, name="request_payout"),
    path("payout/<uuid:payout_id>/receipt/", views.payout_receipt, name="payout_receipt"),
]
