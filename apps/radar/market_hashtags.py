"""Market hashtags: what is in circulation in each line of business.

The operator types hashtags per segment (or for every brand). They are shown
as they are, and once a week each is read on Instagram: the most popular
public posts carrying it, and which other hashtags sit beside it in their
captions. Those come back as ``meta`` rows with how often they appeared.

Meta has no list of trending hashtags; this is the closest it allows. It
needs the Instagram Public Content Access feature approved in App Review and
counts 30 distinct hashtags per account in a rolling seven days, hence the
budget below. Until the feature is approved the collection records Meta's
refusal and the manual hashtags keep being shown.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from providers.exceptions import ProviderError

from . import instagram
from .models import MarketHashtag, MarketHashtagCollector

logger = logging.getLogger(__name__)

#: Seeds read per run. Under Meta's 30 distinct hashtags per account a week,
#: with room for the same account's own searches in the Radar.
SEED_BUDGET = 20

#: How many found hashtags are kept per segment.
FOUND_PER_SEGMENT = 20

_HASHTAG_IN_TEXT = re.compile(r"#(\w{2,100})")


def market_hashtags_for(workspace, *, limit: int = 12) -> list[dict]:
    """The hashtags offered to a brand: those of the segments it follows and
    the global ones. Found on Instagram first, most frequent first, then the
    operator's own. ``[{"tag", "posts", "found"}]``."""
    rows = MarketHashtag.objects.filter(
        Q(segment__isnull=True) | Q(segment__is_active=True, segment__workspace_choices__workspace=workspace)
    ).distinct()
    best: dict[str, MarketHashtag] = {}
    for row in rows:
        current = best.get(row.tag)
        if current is None or row.posts > current.posts:
            best[row.tag] = row
    ranked = sorted(best.values(), key=lambda r: (r.source != MarketHashtag.Source.META, -r.posts, r.tag))
    return [{"tag": r.tag, "posts": r.posts, "found": r.source == MarketHashtag.Source.META} for r in ranked[:limit]]


def add_hashtag(segment, value: str) -> MarketHashtag:
    """Add a hashtag by hand to a segment (``None`` for every brand). Raises
    ``instagram.InvalidInputError`` for something that is not one hashtag.
    One found on Instagram with the same name becomes the operator's."""
    tag = instagram.clean_hashtag(value)
    row, created = MarketHashtag.objects.get_or_create(segment=segment, tag=tag)
    if not created and row.source != MarketHashtag.Source.MANUAL:
        row.source = MarketHashtag.Source.MANUAL
        row.posts = 0
        row.save(update_fields=["source", "posts"])
    return row


def collect() -> str:
    """Read this week's seeds on Instagram and store what was found beside
    them. Returns a line for the log; the outcome is also kept on the
    collector, where the operator sees it.

    The seeds read are the ones waiting longest, so every seed gets its turn
    when there are more than the budget. The first refusal ends the run: it
    is the account or the app that Meta refused, and every other seed would
    spend a request to hear the same.
    """
    collector = MarketHashtagCollector.load()
    account = collector.account
    if account is None:
        return "no account chosen"

    seeds = list(
        MarketHashtag.objects.filter(source=MarketHashtag.Source.MANUAL)
        .filter(Q(segment__isnull=True) | Q(segment__is_active=True))
        .order_by("collected_at", "created_at")[:SEED_BUDGET]
    )
    now = timezone.now()
    found: dict = {}
    error = ""
    for seed in seeds:
        try:
            media = instagram.hashtag_posts(account, seed.tag)
        except instagram.InvalidInputError:
            # Typed before the rules were this strict; nothing to read.
            continue
        except ProviderError as exc:
            error = instagram.describe_error(exc, kind="hashtag")
            logger.warning("Market hashtags: Instagram refused #%s: %s", seed.tag, exc)
            MarketHashtag.objects.filter(pk=seed.pk).update(last_error=error[:300])
            break
        counter = found.setdefault(seed.segment_id, Counter())
        for item in media:
            caption = item.get("caption") or ""
            counter.update({tag.lower() for tag in _HASHTAG_IN_TEXT.findall(caption)})
        MarketHashtag.objects.filter(pk=seed.pk).update(collected_at=now, last_error="")

    for segment_id, counter in found.items():
        _store_found(segment_id, counter)

    collector.last_run_at = now
    collector.last_error = error[:300]
    collector.save(update_fields=["last_run_at", "last_error"])
    return error or f"{len(seeds)} seeds read"


def _store_found(segment_id, counter: Counter) -> None:
    """Replace a segment's found hashtags with the ones of this reading.
    The operator's own hashtags are never overwritten or counted twice."""
    manual = set(
        MarketHashtag.objects.filter(segment_id=segment_id, source=MarketHashtag.Source.MANUAL).values_list(
            "tag", flat=True
        )
    )
    # A hashtag seen in a single post is noise, not a trend.
    top = [(tag, count) for tag, count in counter.most_common() if tag not in manual and count > 1][:FOUND_PER_SEGMENT]
    if not top:
        return
    MarketHashtag.objects.filter(segment_id=segment_id, source=MarketHashtag.Source.META).delete()
    MarketHashtag.objects.bulk_create(
        MarketHashtag(segment_id=segment_id, tag=tag, source=MarketHashtag.Source.META, posts=count)
        for tag, count in top
    )


def collection_status() -> dict:
    """What the operator's screen says about the collection."""
    collector = MarketHashtagCollector.load()
    if collector.account_id is None:
        state = _("No Instagram account chosen: only the hashtags added by hand are shown.")
    elif collector.last_error:
        state = collector.last_error
    elif collector.last_run_at is None:
        state = _("Not read yet. The reading runs once a week, or now with the button.")
    else:
        state = ""
    return {"collector": collector, "state": state, "ok": bool(collector.account_id) and not collector.last_error}
