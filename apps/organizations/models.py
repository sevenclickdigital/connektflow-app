import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _


class Organization(models.Model):
    #: The name every organization is created with at sign-up, until its
    #: owner gives it a real one on the welcome step.
    DEFAULT_NAME = "My Organization"

    # The "about your business" step a new account answers before its plan
    # (organizations.views.about_view), where its name and one more question
    # are asked after the activity (name_question, extra_question).
    class Activity(models.TextChoices):
        ENTREPRENEUR = "entrepreneur", _("Entrepreneur")
        FREELANCER = "freelancer", _("Freelancer")
        AGENCY = "agency", _("Agency")
        MARKETING_ANALYST = "marketing_analyst", _("Marketing analyst")

    class TeamSize(models.TextChoices):
        SOLO = "solo", _("Just me")
        SMALL = "2_5", _("2 to 5 people")
        MEDIUM = "6_20", _("6 to 20 people")
        LARGE = "21_plus", _("More than 20 people")

    class Source(models.TextChoices):
        INSTAGRAM = "instagram", _("Instagram")
        FACEBOOK = "facebook", _("Facebook")
        TIKTOK = "tiktok", _("TikTok")
        YOUTUBE = "youtube", _("YouTube")
        LINKEDIN = "linkedin", _("LinkedIn")
        WHATSAPP = "whatsapp", _("WhatsApp")
        GOOGLE = "google", _("Google search")
        AI_ASSISTANT = "ai_assistant", _("ChatGPT or another AI assistant")
        REFERRAL = "referral", _("A friend or colleague")
        OTHER = "other", _("Somewhere else")

    # Asked on the checkout's billing details, by activity: agencies and
    # freelancers how many clients they serve, businesses and marketing
    # analysts the company's segment.
    class ClientCount(models.TextChoices):
        FEW = "1_5", _("1 to 5 clients")
        SOME = "6_15", _("6 to 15 clients")
        MANY = "16_50", _("16 to 50 clients")
        LOTS = "51_plus", _("More than 50 clients")

    class Segment(models.TextChoices):
        RETAIL = "retail", _("Retail and e-commerce")
        FOOD = "food", _("Food and drink")
        HEALTH = "health", _("Health and wellness")
        BEAUTY = "beauty", _("Beauty and aesthetics")
        EDUCATION = "education", _("Education")
        SERVICES = "services", _("Services")
        REAL_ESTATE = "real_estate", _("Real estate")
        TECHNOLOGY = "technology", _("Technology")
        OTHER = "other", _("Another segment")

    class Goal(models.TextChoices):
        SCHEDULE = "schedule", _("Plan and schedule posts")
        INBOX = "inbox", _("Answer messages and WhatsApp in one place")
        REPORTS = "reports", _("Reports for clients")
        AUTOMATIONS = "automations", _("Automate replies to comments")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    logo_url = models.URLField(blank=True, default="")
    default_timezone = models.CharField(max_length=63, default="UTC")

    # Intelligence billing contact (the optional hosted SaaS integration).
    # Stripe Customer email defaults to this; falls back to the activating
    # user's email if blank. Stored on the Org (not the User) so a change
    # in activator doesn't affect where billing notices land — and so
    # invoices can be routed to a finance address rather than whoever
    # happened to click Subscribe.
    billing_email = models.EmailField(blank=True, default="")

    business_activity = models.CharField(max_length=32, choices=Activity.choices, blank=True, default="", db_default="")
    team_size = models.CharField(max_length=16, choices=TeamSize.choices, blank=True, default="", db_default="")
    referral_source = models.CharField(max_length=16, choices=Source.choices, blank=True, default="", db_default="")
    #: Where, in their own words, when the source picked is "somewhere else".
    referral_source_other = models.CharField(max_length=100, blank=True, default="", db_default="")
    #: Values of ``Goal``; several may be picked.
    goals = models.JSONField(default=list, blank=True, db_default=models.Value([], output_field=models.JSONField()))
    #: When the owner answered the step; it is not shown again after.
    about_answered_at = models.DateTimeField(blank=True, null=True)
    client_count = models.CharField(max_length=16, choices=ClientCount.choices, blank=True, default="", db_default="")
    business_segment = models.CharField(max_length=16, choices=Segment.choices, blank=True, default="", db_default="")

    # Deletion workflow
    deletion_requested_at = models.DateTimeField(blank=True, null=True)
    deletion_scheduled_for = models.DateTimeField(blank=True, null=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations_organization"

    def __str__(self):
        return self.name

    @property
    def is_deletion_pending(self):
        return self.deletion_requested_at is not None and self.deleted_at is None

    @property
    def has_intelligence(self):
        """True when the org has an active Intelligence subscription AND
        the integration is enabled in this deployment.

        Returns False when ``settings.INTELLIGENCE_ENABLED`` is off even
        if a subscription row exists — so a self-hoster who removes the
        env vars doesn't accidentally keep the feature visible. Returns
        False when the related ``IntelligenceSubscription`` doesn't
        exist yet (apps.intelligence may not even be migrated on a
        deployment that hasn't opted in).

        The decorator ``@intelligence_subscription_required`` in
        apps.intelligence layers a stricter check (must be exactly
        ``status='active'``) for tool-call endpoints.
        """
        if not getattr(settings, "INTELLIGENCE_ENABLED", False):
            return False
        sub = getattr(self, "intelligence_subscription", None)
        return bool(sub and sub.status == "active")

    @property
    def goal_labels(self):
        """The goals picked on the "about your business" step, as their labels."""
        labels = dict(self.Goal.choices)
        return [labels[goal] for goal in self.goals if goal in labels]

    @classmethod
    def name_question(cls, activity):
        """How the organization's name is asked after its activity: (label, placeholder).

        Asked as the agency's, the company's, the name a freelancer's clients
        know them by, or the employer's; generic without an activity.
        """
        questions = {
            cls.Activity.AGENCY: (_("Your agency's name"), _("e.g. Sol Agency")),
            cls.Activity.FREELANCER: (_("The name your clients know you by"), _("e.g. Ana Souza Social Media")),
            cls.Activity.ENTREPRENEUR: (_("Your company's name"), _("e.g. Sol Bakery")),
            cls.Activity.MARKETING_ANALYST: (_("The name of the company you work for"), _("e.g. Sol Stores")),
        }
        return questions.get(activity, (_("Company or agency name"), _("e.g. Sol Agency")))

    @classmethod
    def extra_question(cls, activity):
        """The question asked after the activity: (field, label), or None without one.

        How many clients an agency or a freelancer serves; the segment of a
        business, or of the company a marketing analyst works for.
        """
        clients = ("client_count", _("How many clients do you serve today?"))
        return {
            cls.Activity.AGENCY: clients,
            cls.Activity.FREELANCER: clients,
            cls.Activity.ENTREPRENEUR: ("business_segment", _("What is your business's segment?")),
            cls.Activity.MARKETING_ANALYST: ("business_segment", _("What is the company's segment?")),
        }.get(activity)

    @property
    def is_unnamed(self):
        """True while the organization still has the name it was created with."""
        return self.name == self.DEFAULT_NAME

    def hard_delete(self, requesting_user=None, *, departing_user=None):
        """Hard-delete this org and settle every member's account.

        - `requesting_user` (optional): the user who triggered the immediate-
          delete flow. When this was their only organization their account
          is deleted along with it, so they get a truly fresh start on
          re-signup; one who belongs to another organization keeps the
          account. Pass `None` (the default) for
          the scheduled-grace-period path and the management-command sweeper
          — those don't carry request context and we don't want to silently
          delete accounts from a background job.
        - `departing_user` (optional): a member who is being deleted right
          after (``apps.accounts.deletion.delete_account``); they leave with the
          cascade and get no new organization.
        - Every other member gets a freshly provisioned default "My
          Organization" + "My brand" so they land on a working dashboard
          instead of limbo on next login.
        - Any user's `last_workspace_id` that points at a workspace in this
          org is nulled first (defensive; provision-for-member would reset it
          anyway, but the delete_user path wouldn't).
        - Its workspaces' Asaas subscriptions are cancelled before anything is
          deleted (``contracting.cancel_organization_subscriptions``). If the
          gateway refuses, this raises and nothing is deleted: keeping the
          account beats charging for one that is gone.
        """
        from apps.accounts.models import User
        from apps.accounts.signals import provision_organization_and_workspace
        from apps.billing.contracting import cancel_organization_subscriptions
        from apps.media_library.services import schedule_stored_file_removal, tenant_stored_files
        from apps.members.models import OrgMembership
        from apps.workspaces.models import Workspace

        cancel_organization_subscriptions(self)

        member_ids = list(
            OrgMembership.objects.filter(organization=self)
            .exclude(user_id=departing_user.pk if departing_user else None)
            .values_list("user_id", flat=True)
        )
        ws_ids = list(Workspace.objects.filter(organization=self).values_list("id", flat=True))

        if ws_ids:
            User.objects.filter(last_workspace_id__in=ws_ids).update(last_workspace_id=None)

        # The cascade removes rows only; the files are queued for removal in the
        # same transaction, from names read while the rows still exist.
        with transaction.atomic():
            schedule_stored_file_removal(tenant_stored_files(ws_ids, organization_id=self.pk))
            self.delete()  # CASCADE: workspaces, memberships, credentials, media, etc.

        requesting_user_id = requesting_user.pk if requesting_user else None
        for uid in member_ids:
            if uid == requesting_user_id and not OrgMembership.objects.filter(user_id=uid).exists():
                User.objects.filter(pk=uid).delete()
                continue
            try:
                user = User.objects.get(pk=uid)
            except User.DoesNotExist:
                continue
            provision_organization_and_workspace(user)
