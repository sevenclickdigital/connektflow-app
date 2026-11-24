import pytest
from django.utils import timezone

from apps.accounts import operators
from apps.accounts.models import User


def make_customer(email):
    return User.objects.create_user(
        email=email, password="x", name=email.split("@")[0].title(), tos_accepted_at=timezone.now()
    )


@pytest.fixture
def ana(db):
    return make_customer("ana@example.com")


@pytest.fixture
def bruno(db):
    return make_customer("bruno@example.com")


@pytest.fixture
def agent(db):
    return operators.create_operator("agent@example.com", password="x")


@pytest.fixture
def other_agent(db):
    return operators.create_operator("other-agent@example.com", password="x")
