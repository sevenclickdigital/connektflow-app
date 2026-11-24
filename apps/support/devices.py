"""The browser, system and kind of device behind a User-Agent, as the chat console shows them.

A short read of the families support asks about, not a full parser: what is
not recognised is reported as empty, and the raw string is kept beside it
(``Chat.user_agent``) for the rare case it matters. Order matters: Edge,
Opera and Samsung Internet carry "Chrome" in theirs, and Chrome carries
"Safari".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BROWSERS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/(\d+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/(\d+)")),
    ("Samsung Internet", re.compile(r"SamsungBrowser/(\d+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+)[\d.]* (?:Mobile/\S+ )?Safari/")),
)

_SYSTEMS = (
    ("iPadOS", re.compile(r"iPad")),
    ("iOS", re.compile(r"iPhone|iPod")),
    ("Android", re.compile(r"Android")),
    ("Windows", re.compile(r"Windows NT")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("Linux", re.compile(r"Linux")),
)

DESKTOP = "desktop"
MOBILE = "mobile"
TABLET = "tablet"


@dataclass(frozen=True)
class Device:
    browser: str
    operating_system: str
    kind: str


def describe(user_agent: str) -> Device:
    """What ``user_agent`` says about the visitor's browser (with its major version), system and device."""
    agent = user_agent or ""
    browser = ""
    for name, pattern in _BROWSERS:
        match = pattern.search(agent)
        if match:
            browser = f"{name} {match.group(1)}"
            break
    system = next((name for name, pattern in _SYSTEMS if pattern.search(agent)), "")
    if system == "iPadOS" or (system == "Android" and "Mobile" not in agent) or "Tablet" in agent:
        kind = TABLET
    elif system in ("iOS", "Android") or "Mobi" in agent:
        kind = MOBILE
    else:
        kind = DESKTOP if agent else ""
    return Device(browser=browser, operating_system=system, kind=kind)
