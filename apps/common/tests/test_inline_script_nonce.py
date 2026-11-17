"""Every inline script of the templates carries the page's CSP nonce.

Production enforces the Content-Security-Policy (development only reports
it), so an inline <script> without ``nonce`` works locally and is blocked
online: one such script on the create page took the whole idea board down.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)


def test_inline_scripts_carry_the_csp_nonce():
    missing = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in SCRIPT_TAG.finditer(line):
                attrs = match.group(1)
                # External files and JSON data are not inline code.
                if "src=" in attrs or "application/json" in attrs or "application/ld+json" in attrs:
                    continue
                if "nonce=" not in attrs:
                    missing.append(f"{path.relative_to(TEMPLATES_DIR)}:{number}")
    assert not missing, "Inline <script> without nonce (blocked by the CSP in production): " + ", ".join(missing)
