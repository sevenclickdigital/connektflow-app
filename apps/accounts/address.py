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
