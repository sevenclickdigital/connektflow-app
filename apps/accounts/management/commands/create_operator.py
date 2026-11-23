"""Create (or promote) an account that only administers the product.

    python manage.py create_operator ops@example.com
    python manage.py create_operator ops@example.com --generate-password
    python manage.py create_operator ops@example.com --noinput   # promote only

Three kinds of account, and the difference is what they *own*, never a
flag on the user:

**Operator** — an account with no organization. It has ``is_staff=True``,
so it reaches ``/staff/`` and can impersonate a customer for
support. It is billed nothing because billing is per workspace and it has
no workspace: the exemption is a consequence of owning nothing, not a
rule written anywhere. This command creates exactly that.

**Customer** — an account with an organization and workspaces. Every
workspace costs R$ 50,00/month (``BILLING_WORKSPACE_PRICE_CENTS``),
whether or not the person holding the account also happens to be staff.

**Courtesy** — a specific customer workspace an operator chose not to
charge, granted by hand in the staff console (``grant_courtesy``). A
decision about one workspace, recorded on that workspace, reversible, and
attributed to the operator who made it.

The three are kept apart on purpose. The tempting shortcut — "``is_staff``
means free" — couples support to money: promoting a colleague so they can
help a customer for an afternoon would zero that colleague's own invoice,
and demoting them would start charging a customer who never agreed to a
price change. So the product owner, who is both the operator and a paying
customer, gets two accounts instead of one flag.

The rules about what a team account may own, and the code that creates
and promotes one, live in ``apps.accounts.operators``; the Team screen of
the administrative area uses the same functions.

Idempotent. Run it twice on the same address and the second run promotes
the existing account (and leaves its password alone) instead of failing on
the unique email. An account that belongs to an organization or a
workspace is refused: a customer who also joins the team gets a second
account at another address.
"""

from __future__ import annotations

import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.crypto import get_random_string

from apps.accounts import operators
from apps.accounts.models import User

MAX_PASSWORD_ATTEMPTS = 3


class Command(BaseCommand):
    help = "Create or promote a staff-only operator account, with no organization or workspace."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the operator account.")
        parser.add_argument(
            "--name",
            default="",
            help="Display name for a newly created account. Ignored when promoting.",
        )
        parser.add_argument(
            "--generate-password",
            action="store_true",
            help="Generate a strong password and print it once instead of prompting.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also grant is_superuser (Django admin's full rights). Staff alone is enough for the billing console.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Never prompt. Creating a new account then requires --generate-password.",
        )

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"].strip())
        if not email:
            raise CommandError("An email address is required.")

        existing = User.objects.filter(email__iexact=email).first()
        if existing is not None:
            return self._promote(existing, superuser=options["superuser"])

        password = self._resolve_password(
            email,
            interactive=options["interactive"],
            generate=options["generate_password"],
        )
        return self._create(
            email,
            password=password,
            name=options["name"],
            superuser=options["superuser"],
            generated=options["generate_password"],
        )

    # -- creating -----------------------------------------------------

    def _create(self, email, *, password, name, superuser, generated):
        user = operators.create_operator(email, password=password, name=name, superuser=superuser)

        self.stdout.write(self.style.SUCCESS(f"Operator account created: {user.email}"))
        if generated:
            # Printed once and never stored. The operator is expected to
            # change it or move it into a password manager immediately.
            self.stdout.write(self.style.WARNING(f"Generated password (shown once): {password}"))
        self.stdout.write(
            "No organization and no workspace were created, so this account is billed nothing. "
            "Grant a customer's workspace a courtesy from /staff/ instead of reusing this account."
        )
        return None

    def _resolve_password(self, email, *, interactive, generate):
        if generate:
            return get_random_string(operators.GENERATED_PASSWORD_LENGTH)
        if not interactive:
            raise CommandError(
                "Creating an account without input needs --generate-password, "
                "so the password is never passed on the command line (where it would land in shell history)."
            )
        return self._prompt_password(email)

    def _prompt_password(self, email):
        """Ask twice and validate, the way ``createsuperuser`` does.

        ``getpass`` so the password never echoes, and never a command-line
        argument: an argument would be visible in ``ps`` and recorded in
        shell history.
        """
        for _attempt in range(MAX_PASSWORD_ATTEMPTS):
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Password (again): ")
            if password != confirmation:
                self.stderr.write("The two passwords did not match.")
                continue
            if not password.strip():
                self.stderr.write("Blank passwords are not allowed.")
                continue
            try:
                validate_password(password, user=User(email=email, is_staff=True))
            except ValidationError as error:
                for message in error.messages:
                    self.stderr.write(message)
                continue
            return password
        raise CommandError("Too many failed attempts. No account was created.")

    # -- promoting ----------------------------------------------------

    def _promote(self, user, *, superuser):
        """Make an existing account an operator, idempotently."""
        try:
            changed = operators.promote(user, superuser=superuser)
        except operators.OperatorIsCustomerError as error:
            raise CommandError(
                f"{user.email} belongs to an organization or a workspace, so it cannot be an operator: "
                "a team account owns nothing. Create the operator at a different address."
            ) from error

        if changed:
            self.stdout.write(self.style.SUCCESS(f"{user.email} promoted to operator."))
        else:
            self.stdout.write(f"{user.email} is already an operator. Nothing to do.")
        return None
