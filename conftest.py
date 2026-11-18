import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.storage import reset_cached_client
from apps.members.models import OrgMembership
from apps.organizations.models import Organization


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="test@example.com", password="testpass123", name="Test User", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Test Organization")


@pytest.fixture
def org_owner(db, user, organization):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return user


@pytest.fixture(scope="session", autouse=True)
def _compiled_catalogue():
    """Compile the pt-BR catalogue when its ``.mo`` is missing or older than the ``.po``.

    The ``.mo`` is not in git (the image compiles it), so without this a fresh
    checkout serves only English and every test that asserts the Portuguese
    interface fails, while a stale one serves last week's wording.
    """
    from pathlib import Path

    from django.conf import settings
    from django.core.management import call_command

    for locale_dir in settings.LOCALE_PATHS:
        po = Path(locale_dir) / "pt_BR" / "LC_MESSAGES" / "django.po"
        mo = po.with_suffix(".mo")
        if po.exists() and (not mo.exists() or mo.stat().st_mtime < po.stat().st_mtime):
            call_command("compilemessages", locale=["pt_BR"], verbosity=0)
            break


@pytest.fixture(autouse=True)
def _language_reset():
    """Leave the default language active after every test.

    LocaleMiddleware activates a language for each request and never
    deactivates it, so the thread kept whatever the previous test's last
    request used: a test calling a service directly then rendered in English or
    Portuguese depending on which test ran before it.
    """
    from django.utils import translation

    yield
    translation.deactivate()


@pytest.fixture(autouse=True)
def _fresh_storage_client():
    """Keep the memoized boto3 client from leaking across tests.

    ``apps.media_library.storage`` caches one client for the life of the
    process — the point is to stop django-storages rebuilding a boto3 Session
    per thread. Without this, the first test to touch S3 would pin its client
    and every later ``override_settings`` on a bucket or endpoint would be
    silently ignored.
    """
    reset_cached_client()
    yield
    reset_cached_client()
