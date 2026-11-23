"""HTTP client for the Asaas payment gateway.

The only module in the project that knows Asaas's wire format. Everything
above it speaks in local terms (a workspace, a price in cents) and gets
back plain dicts; nothing above it builds a URL or reads a header.

API shape, confirmed against https://docs.asaas.com :

- Auth is the raw API key in an ``access_token`` header — not a Bearer
  token, and not Basic auth.
- Base URL carries the version: ``https://api.asaas.com/v3`` in
  production, ``https://api-sandbox.asaas.com/v3`` in sandbox. Both are
  configured through ``ASAAS_API_BASE``, which defaults to sandbox.
- ``POST /customers`` → ``{"id": "cus_000005401844", ...}``
- ``GET  /customers?externalReference=...`` → a list envelope,
  ``{"object": "list", "hasMore": …, "totalCount": …, "data": [...]}``
- ``POST /subscriptions`` → ``{"id": "sub_VXJBYgP2u0eO", "status": ...}``
- ``DELETE /subscriptions/{id}`` → ``{"deleted": true, "id": ...}``

Money crosses this boundary twice over, in two different units: cents
inside the app, a decimal number of reais in Asaas's ``value`` field.
:func:`cents_to_value` is the only place that conversion happens.

Every failure mode raises :class:`AsaasError` so callers never have to
know whether the problem was a socket, a 400 or a 500. The distinction
that does matter to a caller — "my request was wrong" vs "Asaas is
down", because only the second is worth retrying — is carried by
:class:`AsaasAPIError.status_code`.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal

import httpx
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Recorded on every row this client creates, so a half-migrated table
# stays unambiguous about which vendor an id belongs to.
GATEWAY = "asaas"

# Asaas's recurrence value for a monthly subscription.
CYCLE_MONTHLY = "MONTHLY"
CYCLE_YEARLY = "YEARLY"

# Let the payer pick between card, boleto and Pix on the Asaas checkout
# instead of us committing them to one method up front. Asaas treats
# UNDEFINED as exactly that choice.
BILLING_TYPE_UNDEFINED = "UNDEFINED"
BILLING_TYPE_CREDIT_CARD = "CREDIT_CARD"

#: How long a card charge may take before it is given up as unknown.
CARD_CHARGE_TIMEOUT_SECONDS = 60

#: How long the Pix Automático QR code of a checkout can be paid.
PIX_QR_CODE_SECONDS = 3600

# Asaas's frequencies of a Pix Automático authorization.
PIX_FREQUENCY_MONTHLY = "MONTHLY"
PIX_FREQUENCY_YEARLY = "ANNUALLY"


class AsaasError(Exception):
    """Any failure talking to Asaas. Base of everything below."""


class AsaasNotConfigured(AsaasError):
    """No API key configured.

    Its own class because it is the expected state of a deployment today
    — the credentials have not been issued — and the contracting flow
    wants to tell the user "billing isn't set up yet" rather than report
    a gateway outage.
    """


class AsaasTransportError(AsaasError):
    """The request never produced an HTTP response (DNS, TCP, timeout)."""


class AsaasAPIError(AsaasError):
    """Asaas answered with a non-2xx status.

    ``errors`` holds Asaas's own error list when it sent one; their
    bodies look like ``{"errors": [{"code": ..., "description": ...}]}``.
    """

    def __init__(self, message, *, status_code=None, errors=None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []

    @property
    def is_retryable(self):
        """True when retrying the identical request could succeed.

        A 4xx means we sent something wrong and will keep sending it, so
        only 429 and 5xx qualify.
        """
        if self.status_code is None:
            return False
        return self.status_code == 429 or self.status_code >= 500


def cents_to_value(cents):
    """Integer cents → the decimal reais Asaas's ``value`` field wants.

    Decimal rather than float: ``5000 / 100`` is exact today but the same
    division on an odd number of cents is the classic way a charge ends
    up a hundredth off, and a gateway amount must be exact.
    """
    return Decimal(cents) / Decimal(100)


class AsaasClient:
    """Thin wrapper over the Asaas REST API.

    Instantiated per use rather than kept as a module-level singleton so
    that ``override_settings`` in a test, and a credential rotation in
    production, both take effect without a process restart.
    """

    def __init__(self, api_key=None, api_base=None, timeout=None):
        self.api_key = api_key if api_key is not None else getattr(settings, "ASAAS_API_KEY", "")
        base = api_base if api_base is not None else getattr(settings, "ASAAS_API_BASE", "")
        self.api_base = (base or "").rstrip("/")
        self.timeout = timeout if timeout is not None else getattr(settings, "ASAAS_TIMEOUT_SECONDS", 20)

    @property
    def is_configured(self):
        return bool(self.api_key and self.api_base)

    # -- plumbing ---------------------------------------------------------

    def _request(self, method, path, *, json=None, params=None, timeout=None):
        """Perform one Asaas call and return its decoded body.

        Refuses to leave the process without a key: firing the request
        anyway would just spend a round trip to be told 401, and in the
        sandbox-vs-production mix-up case an unauthenticated request is
        the less useful error message.
        """
        if not self.is_configured:
            raise AsaasNotConfigured("ASAAS_API_KEY/ASAAS_API_BASE are not configured.")

        url = f"{self.api_base}/{path.lstrip('/')}"
        try:
            response = httpx.request(
                method,
                url,
                json=json,
                params=params,
                timeout=timeout or self.timeout,
                headers={
                    # Asaas authenticates on this bespoke header. It is
                    # never logged — see the except branches below, which
                    # deliberately report only method, path and status.
                    "access_token": self.api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "connektflow",
                },
            )
        except httpx.HTTPError as exc:
            # No response at all. Logged without the exception's repr
            # chain, which on some httpx errors includes the request
            # headers and would print the API key.
            logger.warning("Asaas %s %s failed in transport: %s", method, path, type(exc).__name__)
            raise AsaasTransportError(f"Could not reach Asaas ({type(exc).__name__}).") from exc

        if response.status_code >= 400:
            errors = self._parse_errors(response)
            # Asaas error descriptions name the offending field
            # ("cpfCnpj inválido"), never a card number or the key, so
            # they are safe to log and genuinely needed to debug a
            # rejected contract.
            logger.warning("Asaas %s %s returned %s: %s", method, path, response.status_code, errors)
            raise AsaasAPIError(
                f"Asaas returned HTTP {response.status_code}.",
                status_code=response.status_code,
                errors=errors,
            )

        if not response.content:
            # DELETE occasionally answers empty; a caller that only
            # checks for an exception shouldn't have to handle None.
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise AsaasAPIError(
                "Asaas returned a non-JSON body.",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _parse_errors(response):
        """Asaas's ``errors`` list, or [] when the body isn't the usual shape.

        Error bodies are the one response we cannot assume is well
        formed — a 502 from their edge is HTML — so a parse failure here
        must not mask the status code the caller actually needs.
        """
        try:
            body = response.json()
        except ValueError:
            return []
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list):
                return errors
        return []

    # -- customers --------------------------------------------------------

    def create_customer(self, *, name, external_reference, email="", cpf_cnpj="", mobile_phone="", group_name=""):
        """Create a customer (the payer) and return Asaas's object.

        ``external_reference`` carries our Organization id, which is what
        makes :meth:`find_customer_by_reference` — and therefore the
        idempotent contracting flow — possible.
        """
        payload = {"name": name, "externalReference": str(external_reference)}
        # Asaas rejects explicit nulls on these, so send only what we have.
        if email:
            payload["email"] = email
        if cpf_cnpj:
            payload["cpfCnpj"] = cpf_cnpj
        if mobile_phone:
            payload["mobilePhone"] = mobile_phone
        if group_name:
            payload["groupName"] = group_name
        return self._request("POST", "/customers", json=payload)

    def update_customer(self, customer_id, *, name="", email="", cpf_cnpj="", mobile_phone="", group_name=""):
        """Change the payer's data on an existing customer.

        Only what is given is sent: Asaas leaves the other fields as they
        are, and rejects explicit nulls.
        """
        payload = {}
        if name:
            payload["name"] = name
        if email:
            payload["email"] = email
        if cpf_cnpj:
            payload["cpfCnpj"] = cpf_cnpj
        if mobile_phone:
            payload["mobilePhone"] = mobile_phone
        if group_name:
            payload["groupName"] = group_name
        return self._request("PUT", f"/customers/{customer_id}", json=payload)

    def get_customer(self, customer_id):
        return self._request("GET", f"/customers/{customer_id}")

    def find_customer_by_reference(self, external_reference):
        """The customer previously created for this reference, or None.

        The recovery path for a crash between "Asaas created the
        customer" and "we committed its id": without it, a retry would
        create a second customer for the same organization and split its
        payment methods across two.
        """
        body = self._request(
            "GET",
            "/customers",
            params={"externalReference": str(external_reference), "limit": 1},
        )
        data = body.get("data") or []
        return data[0] if data else None

    # -- subscriptions ----------------------------------------------------

    def create_subscription(
        self,
        *,
        customer_id,
        value_cents,
        external_reference,
        description="",
        next_due_date=None,
        cycle=CYCLE_MONTHLY,
        billing_type=BILLING_TYPE_UNDEFINED,
    ):
        """Create a recurring subscription on an existing customer.

        ``next_due_date`` defaults to today: Asaas rejects a past date,
        and billing from today is what the customer just agreed to.
        """
        due = next_due_date or timezone.localdate()
        if isinstance(due, datetime.datetime):
            due = due.date()
        payload = {
            "customer": customer_id,
            "billingType": billing_type,
            "cycle": cycle,
            "value": str(cents_to_value(value_cents)),
            "nextDueDate": due.isoformat(),
            "externalReference": str(external_reference),
        }
        if description:
            # Asaas caps this at 500 characters and 400s past it.
            payload["description"] = description[:500]
        return self._request("POST", "/subscriptions", json=payload)

    def get_subscription(self, subscription_id):
        return self._request("GET", f"/subscriptions/{subscription_id}")

    def find_subscription_by_reference(self, external_reference):
        """The subscription previously created for this reference, or None.

        Same recovery role as :meth:`find_customer_by_reference`: it is
        what stops a retried contract from billing a workspace twice.
        """
        body = self._request(
            "GET",
            "/subscriptions",
            params={"externalReference": str(external_reference), "limit": 1},
        )
        data = body.get("data") or []
        return data[0] if data else None

    def list_subscription_payments(self, subscription_id):
        """The charges Asaas has generated for a subscription.

        Each carries ``status``, ``dueDate`` and ``invoiceUrl`` — the page
        where the customer picks Pix, boleto or card and pays.
        """
        body = self._request("GET", f"/subscriptions/{subscription_id}/payments")
        return body.get("data") or []

    def update_subscription_value(self, subscription_id, value_cents):
        """Change what a subscription charges each cycle.

        ``updatePendingPayments`` carries the new value onto the charge
        already generated and still unpaid, so the next card charge is the
        new figure too.
        """
        return self._request(
            "PUT",
            f"/subscriptions/{subscription_id}",
            json={"value": str(cents_to_value(value_cents)), "updatePendingPayments": True},
        )

    def set_subscription_card(self, subscription_id, *, card_token, remote_ip):
        """Charge a subscription's future cycles to a tokenized card.

        No charge happens now. Asaas switches the subscription to
        ``CREDIT_CARD`` and moves its pending charges to that card.
        """
        return self._request(
            "PUT",
            f"/subscriptions/{subscription_id}/creditCard",
            json={"creditCardToken": card_token, "remoteIp": remote_ip},
        )

    # -- payments ---------------------------------------------------------

    def update_payment_value(self, payment_id, value_cents):
        """Change the value of one unpaid charge (a first-charge coupon)."""
        return self._request("PUT", f"/payments/{payment_id}", json={"value": str(cents_to_value(value_cents))})

    def create_payment(
        self,
        *,
        customer_id,
        value_cents,
        external_reference,
        description="",
        card_token="",
        remote_ip="",
        due_date=None,
    ):
        """Create a one-off charge on an existing customer.

        With ``card_token`` (and the payer's ``remote_ip``) the card is
        charged on the spot, whatever the due date: the answer carries
        ``status`` (``CONFIRMED`` when paid), and a refused card is a 400
        with nothing left at Asaas. Not idempotent, like
        :meth:`pay_with_card`. Without a token the charge waits for the
        payer at its ``invoiceUrl``, where they pick how to pay.
        """
        due = due_date or timezone.localdate()
        payload = {
            "customer": customer_id,
            "billingType": BILLING_TYPE_CREDIT_CARD if card_token else BILLING_TYPE_UNDEFINED,
            "value": str(cents_to_value(value_cents)),
            "dueDate": due.isoformat(),
            "externalReference": str(external_reference),
        }
        if description:
            payload["description"] = description[:500]
        timeout = None
        if card_token:
            payload["creditCardToken"] = card_token
            payload["remoteIp"] = remote_ip
            timeout = max(self.timeout, CARD_CHARGE_TIMEOUT_SECONDS)
        return self._request("POST", "/payments", json=payload, timeout=timeout)

    def find_payment_by_reference(self, external_reference):
        """The one-off charge created for this reference, or None.

        The recovery path for a card charge whose answer was lost: the
        caller cannot tell whether it went through, and must not charge
        again before asking.
        """
        body = self._request(
            "GET",
            "/payments",
            params={"externalReference": str(external_reference), "limit": 1},
        )
        data = body.get("data") or []
        return data[0] if data else None

    def delete_payment(self, payment_id):
        """Withdraw an unpaid one-off charge, so it can no longer be paid."""
        return self._request("DELETE", f"/payments/{payment_id}")

    def pay_with_card(self, payment_id, *, card, holder, remote_ip):
        """Pay one charge with a card, on the spot.

        ``card`` holds ``holderName``, ``number``, ``expiryMonth``,
        ``expiryYear`` and ``ccv``; ``holder`` holds ``name``, ``email``,
        ``cpfCnpj``, ``postalCode``, ``addressNumber`` and ``phone``, as
        Asaas names them. ``remote_ip`` is the payer's, never the server's.

        Not idempotent: after a transport error the charge may or may not
        have gone through, so the caller must not simply call again. A
        refused card is a 400 whose ``errors`` say why. The answer carries
        ``status`` (``CONFIRMED`` when paid) and ``creditCard`` with the
        ``creditCardToken`` that later charges use; when the issuer asks
        for authentication, ``threeDSecureChallengeUrl`` is where the payer
        completes it.
        """
        return self._request(
            "POST",
            f"/payments/{payment_id}/payWithCreditCard",
            json={"creditCard": card, "creditCardHolderInfo": holder, "remoteIp": remote_ip},
            # The card network is slower than the rest of the API (the sandbox
            # took over 20 s), and a timeout here leaves the charge unknown.
            timeout=max(self.timeout, CARD_CHARGE_TIMEOUT_SECONDS),
        )

    def cancel_subscription(self, subscription_id):
        """Stop a subscription's future charges.

        DELETE is Asaas's cancel: it drops pending and overdue charges
        and leaves settled ones on record. Already-paid invoices are not
        refunded, which is the behaviour we want — the customer keeps the
        period they paid for.
        """
        return self._request("DELETE", f"/subscriptions/{subscription_id}")

    # -- Pix Automático -----------------------------------------------------

    def create_pix_automatic_authorization(
        self,
        *,
        customer_id,
        frequency,
        contract_id,
        start_date,
        value_cents,
        first_value_cents,
        description="",
        expiration_seconds=PIX_QR_CODE_SECONDS,
    ):
        """Ask the payer to pay the first charge and authorize the next ones, in one QR code.

        Asaas's "journey 3": the QR code (``encodedImage``, a base64 PNG,
        and ``payload``, the copy-and-paste code) pays ``first_value_cents``
        now; once paid, the payer's bank confirms the authorization and the
        ``PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED`` webhook arrives.
        ``paymentCreationMode=SUBSCRIPTION`` has Asaas charge ``value_cents``
        every ``frequency`` (``MONTHLY`` or ``ANNUALLY``) from
        ``start_date`` through the subscription it creates
        (``subscriptionId``). ``contract_id`` and ``description`` are capped
        at 35 characters by Asaas.
        """
        payload = {
            "customerId": customer_id,
            "frequency": frequency,
            "contractId": str(contract_id)[:35],
            "startDate": start_date.isoformat(),
            # Numbers, not strings: this endpoint refuses "480.00" ("parse_error").
            "value": float(cents_to_value(value_cents)),
            "paymentCreationMode": "SUBSCRIPTION",
            "immediateQrCode": {
                "expirationSeconds": expiration_seconds,
                "originalValue": float(cents_to_value(first_value_cents)),
            },
        }
        if description:
            payload["description"] = description[:35]
        return self._request("POST", "/pix/automatic/authorizations", json=payload)

    def get_pix_automatic_authorization(self, authorization_id):
        """An authorization with its ``status``: CREATED, ACTIVE, CANCELLED, REFUSED or EXPIRED."""
        return self._request("GET", f"/pix/automatic/authorizations/{authorization_id}")

    def cancel_pix_automatic_authorization(self, authorization_id):
        """Withdraw an authorization: its QR code stops working and nothing more is charged."""
        return self._request("DELETE", f"/pix/automatic/authorizations/{authorization_id}")
