import math
import time

from allauth.account.views import ConfirmEmailVerificationCodeView, LoginView, SignupView
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.views import SignupView as SocialSignupView
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator

from .signup_links import custom_signup_url, pending_invite_email, signup_allowed


class InvitePrefillSignupView(SignupView):
    """Signup view that pre-fills and locks the email when a pending
    invite token is in the session."""

    def get_initial(self):
        initial = super().get_initial()
        email = pending_invite_email(self.request)
        if email:
            initial["email"] = email
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["invited_email_locked"] = bool(pending_invite_email(self.request))
        return ctx

    def form_valid(self, form):
        # allauth only catches ImmediateHttpResponse around completing the
        # signup, not around saving the user, where an invite-mode signup
        # that lost its invitation to a concurrent one is refused.
        try:
            return super().form_valid(form)
        except ImmediateHttpResponse as e:
            return e.response


class InviteClaimSocialSignupView(SocialSignupView):
    """allauth's third-party signup form, refusing cleanly when the invite was used meanwhile.

    Reached when a social auto-signup cannot complete on its own (e.g. the
    provider gave no usable email); see ``InvitePrefillSignupView.form_valid``.
    """

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ImmediateHttpResponse as e:
            return e.response


class SignupLinkLoginView(LoginView):
    """Login view whose "Sign up" link honors ``CUSTOM_SIGNUP_URL`` and ``SIGNUP_MODE``.

    allauth's ``signup_url`` (with its ``?next=`` passthrough) is kept when the
    setting is empty or an invite is pending. The link is hidden only when it
    would lead to the closed-signup notice: no custom URL, and this visitor may
    not sign up.
    """

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["signup_url"] = custom_signup_url(self.request) or ctx.get("signup_url")
        ctx["show_signup_link"] = bool(settings.CUSTOM_SIGNUP_URL) or signup_allowed(self.request)
        return ctx


#: Seconds between two codes sent to the same person on the verification page.
RESEND_COOLDOWN = 60


@method_decorator(login_not_required, name="dispatch")
class VerificationCodeView(ConfirmEmailVerificationCodeView):
    """The page where a new account types its e-mail code, with a pause between codes.

    Every new code replaces the one before, so asking again while a mail is
    still on its way leaves the person typing a code that no longer counts.
    A new one is sent only ``RESEND_COOLDOWN`` after the last; the page
    counts the wait down (``resend_wait``) and a request that arrives
    earlier sends nothing and keeps the current code.
    """

    def _resend_wait(self):
        state = self._process.state
        return max(0, math.ceil(state.get("sent_at", state["at"]) + RESEND_COOLDOWN - time.time()))

    def _mark_sent(self):
        self._process.state["sent_at"] = time.time()
        self._process.persist()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["resend_wait"] = self._resend_wait()
        return context

    def _resend_form_valid(self, form):
        if self._resend_wait():
            return HttpResponseRedirect(self.passthrough_next_url(reverse("account_email_verification_sent")))
        response = super()._resend_form_valid(form)
        self._mark_sent()
        return response

    def _change_form_valid(self, form):
        response = super()._change_form_valid(form)
        self._mark_sent()
        return response
