from django.urls import path

from . import views, views_impersonation
from .views_signup import (
    InviteClaimSocialSignupView,
    InvitePrefillSignupView,
    SignupLinkLoginView,
    VerificationCodeView,
)

app_name = "accounts"

urlpatterns = [
    path("login/", SignupLinkLoginView.as_view(), name="account_login"),
    path("signup/", InvitePrefillSignupView.as_view(), name="account_signup"),
    path("3rdparty/signup/", InviteClaimSocialSignupView.as_view(), name="socialaccount_signup"),
    path("confirm-email/", VerificationCodeView.as_view(), name="account_email_verification_sent"),
    path("accept-terms/", views.accept_terms, name="accept_terms"),
    path("popup-done/", views.login_popup_done, name="login_popup_done"),
    path("settings/", views.account_settings, name="settings"),
    path("settings/cep/", views.cep_lookup, name="cep_lookup"),
    path("logout/", views.logout_view, name="logout"),
    path("review-banner/dismiss/", views.dismiss_review_banner, name="dismiss_review_banner"),
    # Operator support impersonation. POST only — see views_impersonation.
    # The kwarg is ``user_id`` and not ``workspace_id``: nothing here is
    # workspace-scoped, and apps.members.middleware would reject the
    # request outright for a workspace the operator is not a member of.
    path(
        "impersonate/<uuid:user_id>/",
        views_impersonation.start_impersonation,
        name="start_impersonation",
    ),
    path(
        "impersonate/stop/",
        views_impersonation.stop_impersonation,
        name="stop_impersonation",
    ),
]
