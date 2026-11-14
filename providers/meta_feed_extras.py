"""The optional fields a Meta post can carry.

A Facebook Page post: place and feeling. An Instagram post: collaborators,
tagged people and location (``apply_instagram_extras``).

Both ride on ``PublishContent.extra`` rather than the publish signature, the
same way ``page_id`` and ``post_type`` do, so adding them touched no provider
that does not support them.

What is deliberately absent: tagging people. ``POST /{page-id}/feed`` still
documents a ``tags`` parameter, but the endpoint that discovered taggable
profiles (``/{user-id}/taggable_friends``) was removed in the post-Cambridge
Analytica lockdown, and ``tags`` cannot be sent without a ``place`` anyway.
There is no way to offer a person picker, so the composer does not pretend to.
"""

from __future__ import annotations

import re

# A feeling is two Open Graph ids, not one: an *action* (what the Page is
# doing) and an *object* (what it is doing it to). "Feeling happy" is the
# FEELS action pointing at the HAPPY object.
FEELS_ACTION_ID = "383634835006146"

# The feeling objects we offer, as (object id, label).
#
# These have to be hardcoded. Meta documents og_object_id but publishes no
# endpoint that lists the available objects, so there is nothing to read them
# from at runtime. Meta's own list runs to several hundred entries; this is
# the handful a business Page plausibly uses, because every entry here is a
# maintenance liability if Meta ever renumbers them.
#
# Sourced from Meta's feelings reference. An id the API rejects costs the
# feeling, never the post: the publish retries once without the decoration
# (``strip_feed_extras``), so a stale entry here degrades rather than fails.
FEELINGS: tuple[tuple[str, str], ...] = (
    ("528297480516636", "happy"),
    ("525497104142297", "blessed"),
    ("123103951186111", "loved"),
    ("308167675961412", "excited"),
    ("118365354995398", "grateful"),
    ("443609379021593", "thankful"),
    ("136050896551329", "accomplished"),
    ("480134215368252", "amazed"),
    ("539354326075059", "inspired"),
    ("379355252154431", "proud"),
    ("304503342999403", "confident"),
    ("112626832239338", "peaceful"),
    ("175500129263221", "content"),
    ("477906418922117", "joyful"),
)

FEELING_IDS = frozenset(object_id for object_id, _label in FEELINGS)


def feeling_label(object_id: str) -> str:
    """The label for ``object_id``, or "" when it isn't one we offer."""
    for known_id, label in FEELINGS:
        if known_id == object_id:
            return label
    return ""


PLACE_SEARCH_FIELDS = "id,name,location,link"
PLACE_SEARCH_LIMIT = 10


def parse_place_results(data: dict) -> list[dict]:
    """Flatten ``/pages/search`` results into what the picker renders.

    ``where`` is the address as a human reads it, assembled from whichever
    location parts Meta returned — many Pages carry only a city.

    A Page with no ``location`` is left out: the search matches any Page by
    name, but only one with location data can be a place. Instagram refuses
    any other as ``location_id`` (``INVALID_LOCATION_ID``), which fails the
    whole publish.
    """
    results = []
    for row in (data or {}).get("data", []) or []:
        page_id = str(row.get("id") or "")
        name = row.get("name") or ""
        location = row.get("location") or {}
        if not page_id or not name or not location:
            continue
        parts = [location.get(key) for key in ("street", "city", "state", "country")]
        results.append(
            {
                "id": page_id,
                "name": name,
                "where": ", ".join(part for part in parts if part),
            }
        )
    return results


# The keys ``apply_feed_extras`` can add. Used to strip the decoration back off
# a payload when the API refuses it, so the retry carries the post without it.
FEED_EXTRA_PAYLOAD_KEYS = ("place", "og_action_type_id", "og_object_id")


def apply_feed_extras(payload: dict, extra: dict) -> dict:
    """Add ``place`` and the feeling fields to a Page feed payload.

    Mutates and returns ``payload``. Anything absent or unrecognised is
    skipped: these decorate a post, and a stale feeling id must not be the
    reason a publish fails.

    Only valid on ``POST /{page-id}/feed``. ``/photos`` and ``/videos`` answer a
    bare ``OAuthException code 1`` (HTTP 500) when these fields are present —
    see ``has_feed_extras``, which callers use to route a decorated photo post
    through ``/feed`` instead.
    """
    place_id = str(extra.get("place_id") or "").strip()
    if place_id:
        payload["place"] = place_id

    object_id = str(extra.get("feeling_object_id") or "").strip()
    if object_id in FEELING_IDS:
        payload["og_action_type_id"] = FEELS_ACTION_ID
        payload["og_object_id"] = object_id
    return payload


def has_feed_extras(extra: dict) -> bool:
    """Whether ``extra`` carries a decoration ``apply_feed_extras`` would add.

    Mirrors the two conditions in ``apply_feed_extras`` exactly — an
    unrecognised feeling id is not a decoration, because that function drops it.
    """
    place_id = str((extra or {}).get("place_id") or "").strip()
    object_id = str((extra or {}).get("feeling_object_id") or "").strip()
    return bool(place_id) or object_id in FEELING_IDS


def strip_feed_extras(payload: dict) -> dict:
    """Remove the decoration keys from ``payload``. Mutates and returns it.

    The other half of the promise in ``apply_feed_extras``: when the platform
    refuses a decorated payload, the publish retries without the decoration
    rather than failing the post.
    """
    for key in FEED_EXTRA_PAYLOAD_KEYS:
        payload.pop(key, None)
    return payload


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

# ``collaborators`` takes up to three usernames (Feed image, carousel, Reels;
# never a Story).
IG_MAX_COLLABORATORS = 3
# How many people the composer lets a post tag. Instagram's own cap on a photo
# is 20.
IG_MAX_USER_TAGS = 20
# Instagram usernames: letters, digits, dots and underscores, up to 30.
_IG_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Where a tag sits on the photo, as a fraction of its width and height. The
# API requires a position for every tag on an image; the composer has no
# picker, so tags sit in the centre, where Instagram shows them on a tap.
_TAG_POSITION = 0.5


def parse_instagram_usernames(text, *, limit: int) -> list[str]:
    """Usernames typed as "@ana, @bia joao" → ["ana", "bia", "joao"].

    Accepts a list too. Drops the "@", anything that can't be a username and
    repeats, keeping the order, and stops at ``limit``.
    """
    items = text if isinstance(text, list | tuple) else re.split(r"[\s,;]+", str(text or ""))
    usernames: list[str] = []
    for item in items:
        name = str(item or "").strip().lstrip("@").lower()
        if _IG_USERNAME.match(name) and name not in usernames:
            usernames.append(name)
        if len(usernames) == limit:
            break
    return usernames


def apply_instagram_extras(payload: dict, extra: dict, *, kind: str) -> dict:
    """Add collaborators, tagged people and location to a media container.

    ``kind`` is what the container is: "image", "reel", "story",
    "carousel" (the parent) or "carousel_item" (the first photo of a
    carousel, which carries the tags). Each field goes only where the API
    takes it: collaborators and location on a Feed image, a Reel and the
    carousel parent; tagged people on an image, the carousel's first photo
    and a Story, positioned on images only; a Reel's cover moment
    (``thumb_offset``) and whether it stays off the profile grid
    (``share_to_feed``) on Reels. Mutates and returns ``payload``.

    Unlike the Facebook feeling, nothing here is stripped when Meta refuses
    it: a username that doesn't exist fails the publish with Meta's message,
    so the post never goes out without the people it was meant to credit.
    """
    extra = extra or {}
    collaborators = parse_instagram_usernames(extra.get("collaborators"), limit=IG_MAX_COLLABORATORS)
    if collaborators and kind in ("image", "reel", "carousel"):
        payload["collaborators"] = collaborators

    tagged = parse_instagram_usernames(extra.get("user_tags"), limit=IG_MAX_USER_TAGS)
    if tagged and kind in ("image", "carousel_item"):
        payload["user_tags"] = [{"username": name, "x": _TAG_POSITION, "y": _TAG_POSITION} for name in tagged]
    elif tagged and kind == "story":
        payload["user_tags"] = [{"username": name} for name in tagged]

    location_id = str(extra.get("place_id") or "").strip()
    if location_id and kind in ("image", "reel", "carousel"):
        payload["location_id"] = location_id

    if kind == "reel":
        # The cover is a moment of the video, in milliseconds; without one
        # Instagram uses the first frame.
        cover_ms = extra.get("video_cover_timestamp_ms")
        if isinstance(cover_ms, int) and not isinstance(cover_ms, bool) and cover_ms >= 0:
            payload["thumb_offset"] = cover_ms
        # Instagram shows a Reel on the profile grid unless told otherwise.
        if extra.get("share_to_feed") is False:
            payload["share_to_feed"] = False
    return payload
