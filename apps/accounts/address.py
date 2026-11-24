"""Brazilian postal code (CEP) lookup, through the public ViaCEP service.

Asked from the server and not from the browser: the page's content security
policy only lets scripts call this application.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
LOOKUP_TIMEOUT_SECONDS = 5


class CepLookupError(Exception):
    """ViaCEP could not be reached or answered something unusable."""


def cep_digits(raw):
    """The 8 digits of a CEP however it was typed, or "" when it is not one."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 8 else ""


def lookup_cep(digits):
    """The address ViaCEP has for an 8-digit CEP, or None when it has none.

    Returns ``street``, ``neighborhood``, ``city`` and ``state`` (the two-letter
    code); a CEP that covers a whole town comes with the first two empty.
    Raises :class:`CepLookupError` when the service fails.
    """
    try:
        response = httpx.get(VIACEP_URL.format(cep=digits), timeout=LOOKUP_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("ViaCEP lookup failed for %s: %s", digits, error)
        raise CepLookupError(str(error)) from error

    if not isinstance(data, dict) or data.get("erro"):
        return None
    return {
        "street": data.get("logradouro") or "",
        "neighborhood": data.get("bairro") or "",
        "city": data.get("localidade") or "",
        "state": data.get("uf") or "",
    }


def remember_address(user, postal_code, number):
    """Keep the CEP and number typed elsewhere (the checkout's card form) as the profile's address.

    Only for a profile with no address yet: one the person wrote is never
    replaced, and the card's billing address may well be another. The street,
    neighbourhood, city and state are looked up from the CEP by a background
    task (``tasks.complete_address_from_cep``), so the page that calls this
    never waits for ViaCEP. Returns whether anything was saved.
    """
    from .tasks import complete_address_from_cep

    digits = cep_digits(postal_code)
    number = (number or "").strip()[:20]
    if user.address_postal_code or not digits or not number:
        return False
    user.address_postal_code = f"{digits[:5]}-{digits[5:]}"
    user.address_number = number
    user.save(update_fields=["address_postal_code", "address_number"])
    complete_address_from_cep(str(user.pk))
    return True


def complete_address(user):
    """Fill the street, neighbourhood, city and state the profile lacks, from its CEP.

    What the person already has is kept. Raises :class:`CepLookupError` when
    the service fails. Returns whether anything was filled.
    """
    digits = cep_digits(user.address_postal_code)
    if not digits or user.address_city:
        return False
    found = lookup_cep(digits)
    if not found:
        return False
    filled = {
        "address_street": found["street"][:255],
        "address_neighborhood": found["neighborhood"][:120],
        "address_city": found["city"][:120],
        "address_state": found["state"][:2],
    }
    changed = [name for name, value in filled.items() if value and not getattr(user, name)]
    for name in changed:
        setattr(user, name, filled[name])
    if changed:
        user.save(update_fields=changed)
    return bool(changed)
