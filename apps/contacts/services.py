"""Contacts: the inbox link, engagement, filters, stats and CSV import/export."""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import CharField, Count, F, Func, Max, Q, QuerySet
from django.utils import formats, timezone
from django.utils.timesince import timesince
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .models import ENGAGEMENT_WINDOW_DAYS, ChannelKind, Contact, ContactChannel

logger = logging.getLogger(__name__)

#: Social account platforms whose inbox messages count for each channel kind.
_PLATFORMS_FOR_KIND: dict[str, tuple[str, ...]] = {
    ChannelKind.INSTAGRAM: ("instagram", "instagram_login"),
    ChannelKind.FACEBOOK: ("facebook",),
    ChannelKind.MESSENGER: ("facebook",),
    ChannelKind.LINKEDIN: ("linkedin", "linkedin_personal", "linkedin_company"),
    ChannelKind.WHATSAPP: ("whatsapp",),
    ChannelKind.TIKTOK: ("tiktok",),
    ChannelKind.YOUTUBE: ("youtube",),
    ChannelKind.THREADS: ("threads",),
    ChannelKind.X: ("x",),
    ChannelKind.BLUESKY: ("bluesky",),
    ChannelKind.MASTODON: ("mastodon",),
    ChannelKind.GOOGLE_BUSINESS: ("google_business",),
}

#: Channel kinds whose value is an @handle rather than a phone or address.
HANDLE_KINDS = frozenset(
    {
        ChannelKind.INSTAGRAM,
        ChannelKind.TIKTOK,
        ChannelKind.THREADS,
        ChannelKind.X,
        ChannelKind.TELEGRAM,
        ChannelKind.BLUESKY,
        ChannelKind.MASTODON,
    }
)

#: How many inbox senders one refresh pass reads. The pass runs daily in the
#: shared worker lane, so it stays bounded; the most recent senders go first.
REFRESH_SENDER_CAP = 5000

PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Channel values
# ---------------------------------------------------------------------------


def normalize_channel_value(kind: str, value: str) -> str:
    """The stored form of a channel value: ``+digits`` for phones, ``@handle``
    for handle networks, a lower-cased address for email, trimmed otherwise."""
    value = (value or "").strip()
    if not value:
        return ""
    if kind in (ChannelKind.WHATSAPP, ChannelKind.PHONE):
        digits = re.sub(r"\D", "", value)
        return f"+{digits}" if digits else ""
    if kind == ChannelKind.EMAIL:
        return value.lower()
    if kind in HANDLE_KINDS:
        return "@" + value.lstrip("@")
    return value


def _inbox_identifier(kind: str, value: str) -> str:
    """What the inbox stores as ``sender_handle`` for a channel value."""
    if kind == ChannelKind.WHATSAPP:
        return re.sub(r"\D", "", value)
    return value.lstrip("@")


def channel_kind_for(platform: str, message_type: str) -> str:
    """The channel kind an inbox message reaches us through, or "" when none fits."""
    if platform == "facebook" and message_type == "dm":
        return ChannelKind.MESSENGER
    for kind, platforms in _PLATFORMS_FOR_KIND.items():
        if kind != ChannelKind.MESSENGER and platform in platforms:
            return kind
    return ""


# ---------------------------------------------------------------------------
# Inbox link
# ---------------------------------------------------------------------------


def on_inbox_message_created(message) -> None:
    """Turn the sender of a newly stored inbox message into a contact.

    Called from the two places a message is born (the webhook handler and the
    polling engine), next to the automations hook. **Never raises**: a failure
    here must not cost the inbox the customer's message, so it runs after the
    commit and only logs. The daily ``refresh_contacts`` pass picks up any
    sender this missed.
    """
    message_id = message.pk

    def _record():
        try:
            record_inbox_sender(message_id)
        except Exception:
            logger.exception("Could not record the sender of inbox message %s as a contact", message_id)

    try:
        transaction.on_commit(_record)
    except Exception:
        logger.exception("Could not schedule the contact for inbox message %s", message_id)


def record_inbox_sender(message_id) -> Contact | None:
    """Find or create the contact behind an inbox message and refresh its numbers.

    Idempotent: the same message, or any later one from the same person, lands
    on the same contact through the channel's ``external_id``.
    """
    from apps.inbox.models import InboxMessage

    message = InboxMessage.objects.select_related("social_account").filter(pk=message_id).first()
    if message is None or not message.sender_handle:
        return None
    kind = channel_kind_for(message.social_account.platform, message.message_type)
    if not kind:
        return None

    external_id = message.sender_handle
    channel = (
        ContactChannel.objects.select_related("contact")
        .filter(workspace_id=message.workspace_id, kind=kind, external_id=external_id)
        .first()
    )
    if channel is None:
        channel = _claim_typed_channel(message, kind, external_id)
    if channel is None:
        channel = _create_from_message(message, kind, external_id)
    contact = channel.contact
    if not contact.avatar_url and message.sender_avatar_url:
        contact.avatar_url = message.sender_avatar_url
        contact.save(update_fields=["avatar_url", "updated_at"])
    refresh_interactions(contact)
    return contact


def contact_for_message(message) -> Contact | None:
    """The contact behind an inbox message, without creating one.

    Matches like ``record_inbox_sender``: the sender's platform ID first, then
    the channel's value (an @handle typed in or seen through polling).
    """
    if not message.sender_handle:
        return None
    kind = channel_kind_for(message.social_account.platform, message.message_type)
    if not kind:
        return None
    channels = ContactChannel.objects.select_related("contact").filter(workspace_id=message.workspace_id, kind=kind)
    channel = channels.filter(external_id=message.sender_handle).first()
    if channel is None:
        handle = str((message.extra or {}).get("sender_handle") or "")
        raw = {v for v in (message.sender_handle, handle) if v and not v.isdigit()}
        values = (raw | {normalize_channel_value(kind, v) for v in raw}) - {""}
        channel = channels.filter(value__in=values).order_by("created_at").first() if values else None
    return channel.contact if channel else None


def _claim_typed_channel(message, kind: str, external_id: str) -> ContactChannel | None:
    """A channel without this inbox ID whose value names the same sender.

    The same person reaches the inbox under their platform ID (webhooks) or
    their @handle (polling), and the team may have typed the handle by hand;
    matching on the value keeps each of those from becoming a second contact.
    The channel takes the ID when it has none yet.
    """
    handle = str((message.extra or {}).get("sender_handle") or "")
    raw = {v for v in (external_id, handle) if v and not v.isdigit()}
    candidates = (raw | {normalize_channel_value(kind, v) for v in raw}) - {""}
    if not candidates:
        return None
    channel = (
        ContactChannel.objects.select_related("contact")
        .filter(workspace_id=message.workspace_id, kind=kind, value__in=candidates)
        .order_by("created_at")
        .first()
    )
    if channel is not None and not channel.external_id:
        channel.external_id = external_id
        try:
            with transaction.atomic():
                channel.save(update_fields=["external_id"])
        except IntegrityError:
            return None
    return channel


def _create_from_message(message, kind: str, external_id: str) -> ContactChannel:
    handle = str((message.extra or {}).get("sender_handle") or "")
    if kind == ChannelKind.WHATSAPP:
        value = normalize_channel_value(kind, external_id)
    elif handle and not handle.isdigit():
        value = normalize_channel_value(kind, handle) if kind in HANDLE_KINDS else handle
    elif not external_id.isdigit():
        value = normalize_channel_value(kind, external_id) if kind in HANDLE_KINDS else external_id
    else:
        value = message.sender_name or external_id
    source = kind if kind in Contact.Source.values else ChannelKind.FACEBOOK
    try:
        with transaction.atomic():
            contact = Contact.objects.create(
                workspace_id=message.workspace_id,
                name=(message.sender_name or value)[:200],
                source=source,
                avatar_url=message.sender_avatar_url or "",
            )
            return ContactChannel.objects.create(
                workspace_id=message.workspace_id,
                contact=contact,
                kind=kind,
                value=value[:255],
                external_id=external_id,
                is_primary=True,
            )
    except IntegrityError:
        # Another message from the same person won the race; use its contact.
        return ContactChannel.objects.select_related("contact").get(
            workspace_id=message.workspace_id, kind=kind, external_id=external_id
        )


def interactions_queryset(contact: Contact):
    """The inbox messages this contact sent, matched through its channels."""
    from apps.inbox.models import InboxMessage

    match = Q()
    for channel in contact.channels.all():
        platforms = _PLATFORMS_FOR_KIND.get(channel.kind)
        if not platforms:
            continue
        identifiers = {channel.external_id, _inbox_identifier(channel.kind, channel.value)} - {""}
        kind_q = Q(social_account__platform__in=platforms, sender_handle__in=identifiers)
        if channel.kind == ChannelKind.MESSENGER:
            kind_q &= Q(message_type="dm")
        elif channel.kind == ChannelKind.FACEBOOK:
            kind_q &= ~Q(message_type="dm")
        match |= kind_q
    if not match:
        return InboxMessage.objects.none()
    return InboxMessage.objects.filter(match, workspace_id=contact.workspace_id)


def contact_conversations(contact: Contact) -> list:
    """One inbox message per place the contact talks to the workspace, newest first.

    The latest message of each (account, type) the contact wrote in: their DM on
    each network, their latest comment, and so on. Opening one opens its
    conversation, which is how the inbox moves between a person's channels.
    """
    latest = (
        interactions_queryset(contact)
        .select_related("social_account")
        .order_by("social_account_id", "message_type", "-received_at")
        .distinct("social_account_id", "message_type")
    )
    return sorted(latest, key=lambda message: message.received_at, reverse=True)


def other_channels_by_message(workspace_id, messages) -> dict:
    """For each inbox message, the other channels its sender's contact talks through.

    ``{message.id: [kind, …]}`` with the kinds of the contact's channels that
    carry an inbox link, minus the message's own. The list of entries shows them
    beside the sender, so a contact merged across networks reads as one person.
    Two queries for the whole page; senders are matched on the inbox ID only.
    """
    keys = {}
    for message in messages:
        kind = channel_kind_for(message.social_account.platform, message.message_type)
        if kind and message.sender_handle:
            keys[message.id] = (kind, message.sender_handle)
    if not keys:
        return {}
    owners = {
        (row["kind"], row["external_id"]): row["contact_id"]
        for row in ContactChannel.objects.filter(
            workspace_id=workspace_id, external_id__in={handle for _kind, handle in keys.values()}
        ).values("kind", "external_id", "contact_id")
    }
    kinds_by_contact: dict = {}
    for row in (
        ContactChannel.objects.filter(contact_id__in=set(owners.values()))
        .exclude(external_id="")
        .values("contact_id", "kind")
    ):
        kinds_by_contact.setdefault(row["contact_id"], set()).add(row["kind"])
    result = {}
    for message_id, (kind, handle) in keys.items():
        others = kinds_by_contact.get(owners.get((kind, handle)), set()) - {kind}
        if others:
            result[message_id] = sorted(others)
    return result


MERGE_CANDIDATES_LIMIT = 6


def merge_candidates(contact: Contact, query: str = "") -> list[Contact]:
    """Contacts that may be the same person as ``contact``, to merge from the inbox.

    With ``query``, the workspace's contacts the list's search finds. Without
    it, the suggestions: contacts whose name starts with the same first word,
    since the same person on another network usually keeps their first name
    ("Robison F. Borges" on Messenger, "robison.sevenclick" on Instagram).
    Meta gives no link between a person's Instagram and Messenger IDs, so the
    team confirms every merge.
    """
    if query:
        qs = filter_contacts(contact.workspace_id, {"q": query[:100]})
    else:
        first = re.split(r"[\s._\-]+", (contact.name or "").strip().lstrip("@+"))[0]
        if len(first) < 3 or first.isdigit():
            return []
        qs = Contact.objects.for_workspace(contact.workspace_id).filter(name__istartswith=first)
    return list(
        qs.exclude(pk=contact.pk)
        .prefetch_related("channels")
        .order_by(F("last_contact_at").desc(nulls_last=True), "name")[:MERGE_CANDIDATES_LIMIT]
    )


#: Profile fields that count as filled data in ``merge_summary`` (photo and
#: avatar_url count once, as the picture).
_SUMMARY_FIELDS = (
    "tax_id",
    "birth_date",
    "gender",
    "company",
    "funnel",
    "segment",
    "tags",
    "address",
    "city",
    "state",
    "postal_code",
    "notes",
)


def merge_summary(contact: Contact, *, now=None) -> dict:
    """How much data ``contact`` holds, for the inbox's merge dialog.

    Channels come from ``contact.channels.all()``, so prefetch them on lists
    (``merge_candidates`` does). ``score`` ranks the contacts: each channel and
    each filled profile field is a point, and the interactions in the window
    only break ties between equal points. ``text`` is the translated line the
    dialog shows ("3 channels · 12 interactions · 5 fields filled · last contact
    2 days ago").
    """
    channels = len(contact.channels.all())
    fields = sum(1 for name in _SUMMARY_FIELDS if getattr(contact, name))
    fields += 1 if (contact.photo or contact.avatar_url) else 0
    interactions = contact.interactions_90d or 0
    parts = [
        ngettext("%(count)d channel", "%(count)d channels", channels) % {"count": channels},
        ngettext("%(count)d interaction", "%(count)d interactions", interactions) % {"count": interactions},
        ngettext("%(count)d field filled", "%(count)d fields filled", fields) % {"count": fields},
    ]
    if contact.last_contact_at:
        when = timesince(contact.last_contact_at, now or timezone.now(), depth=1)
        parts.append(_("last contact %(time)s ago") % {"time": when})
    return {
        "channels": channels,
        "interactions": interactions,
        "fields": fields,
        "last_contact_at": contact.last_contact_at,
        "score": (channels + fields) * 1000 + min(interactions, 999),
        "text": " · ".join(parts),
    }


def most_complete(summaries: dict) -> object | None:
    """The key of the summary with the strictly highest score, or None on a tie
    for first place (or with fewer than two summaries): the dialog marks a
    contact only when one clearly holds more data."""
    if len(summaries) < 2:
        return None
    ranked = sorted(summaries.items(), key=lambda item: item[1]["score"], reverse=True)
    if ranked[0][1]["score"] == ranked[1][1]["score"]:
        return None
    return ranked[0][0]


def refresh_interactions(contact: Contact, *, now=None) -> None:
    """Recount the contact's interactions in the window and its last contact date."""
    now = now or timezone.now()
    since = now - timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    numbers = interactions_queryset(contact).aggregate(
        recent=Count("id", filter=Q(received_at__gte=since)),
        last=Max("received_at"),
    )
    contact.interactions_90d = numbers["recent"] or 0
    if numbers["last"] and (contact.last_contact_at is None or numbers["last"] > contact.last_contact_at):
        contact.last_contact_at = numbers["last"]
    contact.refresh_engagement()
    contact.save(update_fields=["interactions_90d", "last_contact_at", "engagement", "updated_at"])


def refresh_all(*, now=None) -> int:
    """Daily pass over the inbox: contacts for senders that have none yet (any
    date, so the first run imports the history), a recount for everyone who
    wrote in the window, and decay for those who went quiet.

    Returns how many senders it read.
    """
    from django.db.models import Exists, OuterRef

    from apps.inbox.models import InboxMessage

    now = now or timezone.now()
    since = now - timedelta(days=ENGAGEMENT_WINDOW_DAYS)
    known = ContactChannel.objects.filter(workspace_id=OuterRef("workspace_id"), external_id=OuterRef("sender_handle"))
    senders = InboxMessage.objects.exclude(sender_handle="").filter(Q(received_at__gte=since) | ~Exists(known))
    latest_per_sender = list(
        senders.order_by("workspace_id", "social_account_id", "sender_handle", "-received_at")
        .distinct("workspace_id", "social_account_id", "sender_handle")
        .values_list("id", "received_at")
    )
    latest_per_sender.sort(key=lambda row: row[1], reverse=True)
    read = 0
    for message_id, _received in latest_per_sender[:REFRESH_SENDER_CAP]:
        try:
            record_inbox_sender(message_id)
        except Exception:
            logger.exception("Contact refresh failed for inbox message %s", message_id)
        read += 1

    quiet = Contact.objects.filter(interactions_90d__gt=0).filter(
        Q(last_contact_at__lt=since) | Q(last_contact_at__isnull=True)
    )
    for contact in quiet.iterator():
        contact.interactions_90d = 0
        contact.refresh_engagement()
        contact.save(update_fields=["interactions_90d", "engagement", "updated_at"])
    return read


# ---------------------------------------------------------------------------
# Saving from the form
# ---------------------------------------------------------------------------


@dataclass
class ChannelInput:
    kind: str
    value: str
    is_primary: bool = False


def save_channels(contact: Contact, channels: list[ChannelInput]) -> None:
    """Replace the contact's channels with ``channels``, keeping the inbox link
    (``external_id``) of every channel that survives unchanged."""
    existing = {(c.kind, c.value): c for c in contact.channels.all()}
    keep = []
    for position, item in enumerate(channels):
        current = existing.get((item.kind, item.value))
        if current is None:
            current = ContactChannel(
                workspace_id=contact.workspace_id, contact=contact, kind=item.kind, value=item.value
            )
        current.is_primary = item.is_primary
        current.position = position
        current.save()
        keep.append(current.pk)
    contact.channels.exclude(pk__in=keep).delete()


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

#: Fields a merged contact takes from the others when its own is empty.
_MERGE_FILL_FIELDS = (
    "tax_id",
    "birth_date",
    "gender",
    "company",
    "avatar_url",
    "funnel",
    "segment",
    "engagement_manual",
    "address",
    "city",
    "state",
    "postal_code",
)
MAX_MERGE = 20

#: Characters a person can type in a contact's notes (form and CSV import). A
#: merge keeps every note whole, so a merged contact may hold more.
MAX_NOTES_LENGTH = 5000


def _merge_labels() -> dict[str, str]:
    """The fields whose differing value on a merged contact is written into the
    kept contact's notes, with the label the contact form shows.

    ``avatar_url`` is left out (a platform picture link; the kept photo already
    wins) and so is ``engagement_manual`` (a level set by hand, while the level
    shown is recomputed from the interactions of every merged channel).
    """
    return {
        "kind": _("Contact type"),
        "tax_id": _("CPF / CNPJ"),
        "birth_date": _("Date of birth"),
        "gender": _("Gender"),
        "company": _("Company"),
        "status": _("Status"),
        "funnel": _("Funnel"),
        "segment": _("Segment"),
        "source": _("Contact source"),
        "address": _("Address"),
        "city": _("City"),
        "state": _("State"),
        "postal_code": _("CEP"),
    }


def _merge_display(contact: Contact, name: str) -> str:
    value = getattr(contact, name)
    if name == "birth_date":
        return formats.date_format(value, "SHORT_DATE_FORMAT")
    if Contact._meta.get_field(name).choices:
        return str(getattr(contact, f"get_{name}_display")())
    return str(value)


def _merge_note(keep: Contact, other: Contact) -> str:
    """What ``other`` holds that ``keep`` does not take: its name when it differs
    and each field set to another value, under a "Merged from" line. Empty when
    nothing differs."""
    lines = [
        f"{label}: {_merge_display(other, name)}"
        for name, label in _merge_labels().items()
        if getattr(other, name) and getattr(other, name) != getattr(keep, name)
    ]
    if lines or other.name != keep.name:
        lines.insert(0, _("Merged from %(name)s") % {"name": other.name})
    return "\n".join(lines)


def merge_contacts(workspace_id, keep_id, other_ids) -> Contact:
    """Fold ``other_ids`` into the contact ``keep_id`` and delete them.

    The kept contact receives every channel (with its inbox link, so both
    networks' interactions count for it), the union of the tags, the most
    recent last contact, a photo when it has none, and any field it left
    empty. Nothing else is lost: for each merged contact its notes, whole, go
    under a "Merged from <name>" block listing the name and the fields whose
    value differs from the kept one (``_merge_note``), so the notes may exceed
    ``MAX_NOTES_LENGTH``. Raises ``Contact.DoesNotExist`` when an id is not a
    contact of this workspace.
    """
    others_ids = [str(i) for i in other_ids if str(i) != str(keep_id)]
    with transaction.atomic():
        contacts = {
            str(c.pk): c
            for c in Contact.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk__in=[keep_id, *others_ids])
            .order_by("created_at")
        }
        if str(keep_id) not in contacts or len(contacts) != len({str(keep_id), *others_ids}):
            raise Contact.DoesNotExist
        keep = contacts.pop(str(keep_id))
        others = list(contacts.values())
        notes = [keep.notes] if keep.notes else []
        has_primary = keep.channels.filter(is_primary=True).exists()
        position = keep.channels.count()
        for other in others:
            for name in _MERGE_FILL_FIELDS:
                if not getattr(keep, name) and getattr(other, name):
                    setattr(keep, name, getattr(other, name))
            if not keep.photo and other.photo:
                keep.photo = other.photo.name
                other.photo = ""
            keep.tags = clean_tags([*(keep.tags or []), *(other.tags or [])])
            if other.last_contact_at and (keep.last_contact_at is None or other.last_contact_at > keep.last_contact_at):
                keep.last_contact_at = other.last_contact_at
            notes.extend(part for part in (_merge_note(keep, other), other.notes) if part)
            for channel in other.channels.order_by("position", "created_at"):
                same = keep.channels.filter(kind=channel.kind, value=channel.value).first()
                if same is not None:
                    # The kept channel takes the inbox link when it has none; the
                    # duplicate goes first, the link being unique per workspace.
                    if channel.external_id and not same.external_id:
                        same.external_id = channel.external_id
                        channel.delete()
                        same.save(update_fields=["external_id"])
                    continue
                channel.contact = keep
                channel.position = position
                channel.is_primary = channel.is_primary and not has_primary
                has_primary = has_primary or channel.is_primary
                channel.save(update_fields=["contact", "position", "is_primary"])
                position += 1
        keep.notes = "\n\n".join(notes)
        keep.save()
        for other in others:
            if other.photo:
                other.photo.delete(save=False)
            other.delete()
        refresh_interactions(keep)
    return keep


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

LAST_CONTACT_RANGES = ("7", "30", "90", "older", "never")


def channel_kinds_for_filter(code: str) -> list[str]:
    """The channel kinds a list filter covers. ``code`` is a connected platform
    (the channel pills: Facebook also covers Messenger, Instagram Login is
    Instagram) or a channel kind."""
    kinds = [kind for kind, platforms in _PLATFORMS_FOR_KIND.items() if code in platforms]
    if not kinds and code in ChannelKind.values:
        kinds = [code]
    return kinds


def filter_contacts(workspace_id, params) -> QuerySet[Contact]:
    """The workspace's contacts under the list's filters (a ``QueryDict`` or dict)."""
    qs = Contact.objects.for_workspace(workspace_id)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(company__icontains=q) | Q(channels__value__icontains=q) | Q(notes__icontains=q)
        ).distinct()
    kinds = channel_kinds_for_filter(params.get("channel") or "")
    if kinds:
        qs = qs.filter(channels__kind__in=kinds).distinct()
    for name, field_name, choices in (
        ("status", "status", Contact.Status.values),
        ("source", "source", Contact.Source.values),
        ("engagement", "engagement", Contact.Engagement.values),
    ):
        value = params.get(name)
        if value in choices:
            qs = qs.filter(**{field_name: value})
    for name in ("funnel", "segment"):
        value = (params.get(name) or "").strip()
        if value:
            qs = qs.filter(**{f"{name}__iexact": value})
    tag = (params.get("tag") or "").strip()
    if tag:
        qs = qs.filter(tags__contains=[tag])
    last = params.get("last")
    if last in ("7", "30", "90"):
        qs = qs.filter(last_contact_at__gte=timezone.now() - timedelta(days=int(last)))
    elif last == "older":
        qs = qs.filter(last_contact_at__lt=timezone.now() - timedelta(days=90))
    elif last == "never":
        qs = qs.filter(last_contact_at__isnull=True)
    return qs


def facet_values(workspace_id) -> dict[str, list[str]]:
    """The funnels, segments and tags in use, for the filters and the form's suggestions."""
    qs = Contact.objects.for_workspace(workspace_id)
    funnels = sorted({v for v in qs.exclude(funnel="").values_list("funnel", flat=True)}, key=str.lower)
    segments = sorted({v for v in qs.exclude(segment="").values_list("segment", flat=True)}, key=str.lower)
    tags = {
        t
        for t in qs.exclude(tags=[])
        .annotate(tag=Func(F("tags"), function="jsonb_array_elements_text", output_field=CharField()))
        .values_list("tag", flat=True)
        .distinct()
    }
    return {"funnels": funnels, "segments": segments, "tags": sorted(tags, key=str.lower)}


def stats(workspace_id) -> dict:
    """The numbers on top of the list, over all of the workspace's contacts."""
    since = timezone.now() - timedelta(days=30)
    numbers = Contact.objects.for_workspace(workspace_id).aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=Contact.Status.ACTIVE)),
        new=Count("id", filter=Q(created_at__gte=since)),
        engaged=Count("id", filter=Q(engagement__in=[Contact.Engagement.HIGH, Contact.Engagement.MEDIUM])),
        funnels=Count("funnel", filter=~Q(funnel=""), distinct=True),
    )
    total = numbers["total"]
    numbers["engagement_rate"] = round(numbers["engaged"] * 100 / total) if total else 0
    return numbers


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

#: Column order of the export, which the import reads back.
CSV_COLUMNS = (
    "name",
    "kind",
    "whatsapp",
    "instagram",
    "email",
    "phone",
    "company",
    "status",
    "funnel",
    "segment",
    "tags",
    "source",
    "city",
    "state",
    "notes",
)
_CSV_CHANNELS = ("whatsapp", "instagram", "email", "phone")
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000


def export_csv(contacts) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for contact in contacts.prefetch_related("channels"):
        by_kind: dict[str, str] = {}
        for channel in contact.channels.all():
            by_kind.setdefault(channel.kind, channel.value)
        row = {
            "name": contact.name,
            "kind": contact.kind,
            "company": contact.company,
            "status": contact.status,
            "funnel": contact.funnel,
            "segment": contact.segment,
            "tags": ", ".join(contact.tags or []),
            "source": contact.source,
            "city": contact.city,
            "state": contact.state,
            "notes": contact.notes,
            **{kind: by_kind.get(kind, "") for kind in _CSV_CHANNELS},
        }
        writer.writerow([_csv_safe(row[col]) for col in CSV_COLUMNS])
    return out.getvalue()


def _csv_safe(value: str) -> str:
    """Keep a spreadsheet from running a cell as a formula."""
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@") and not re.fullmatch(r"\+?\d[\d ]*", value) else value


@dataclass
class ImportResult:
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def import_csv(workspace_id, raw: bytes, *, user=None) -> ImportResult:
    """Create a contact for each row of a CSV with the export's columns.

    Only ``name`` is required. A row whose WhatsApp, Instagram or email already
    belongs to a contact is skipped rather than duplicated.
    """
    result = ImportResult()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.errors.append(_("The file must be a CSV saved as UTF-8."))
        return result
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        result.errors.append(_('The first line must name the columns, and one of them must be "name".'))
        return result

    for number, raw_row in enumerate(reader, start=2):
        if number - 1 > MAX_IMPORT_ROWS:
            result.errors.append(_("Only the first %(count)s rows were read.") % {"count": MAX_IMPORT_ROWS})
            break
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        name = row.get("name", "")[:200]
        if not name:
            result.skipped += 1
            continue
        channels = [
            ChannelInput(kind, normalize_channel_value(kind, row.get(kind, "")))
            for kind in _CSV_CHANNELS
            if normalize_channel_value(kind, row.get(kind, ""))
        ]
        if (
            channels
            and ContactChannel.objects.filter(
                workspace_id=workspace_id,
                **{"kind__in": [c.kind for c in channels], "value__in": [c.value for c in channels]},
            ).exists()
        ):
            result.skipped += 1
            continue
        if channels:
            channels[0].is_primary = True
        with transaction.atomic():
            contact = Contact(
                workspace_id=workspace_id,
                name=name,
                kind=_choice(row.get("kind", ""), Contact.Kind.values, Contact.Kind.PERSON),
                company=row.get("company", "")[:200],
                status=_choice(row.get("status", ""), Contact.Status.values, Contact.Status.LEAD),
                funnel=row.get("funnel", "")[:100],
                segment=row.get("segment", "")[:100],
                tags=clean_tags(row.get("tags", "")),
                source=_choice(row.get("source", ""), Contact.Source.values, Contact.Source.IMPORT),
                city=row.get("city", "")[:120],
                state=row.get("state", "")[:2].upper(),
                notes=row.get("notes", "")[:MAX_NOTES_LENGTH],
                created_by=user,
            )
            contact.save()
            save_channels(contact, channels)
            refresh_interactions(contact)
        result.created += 1
    return result


def _choice(value: str, allowed, default: str) -> str:
    return value if value in allowed else default


MAX_TAGS = 20
MAX_TAG_LENGTH = 40


def clean_tags(raw) -> list[str]:
    """Tags from a comma-separated string or a list: trimmed, unique, bounded."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    tags: list[str] = []
    for item in items:
        tag = str(item).strip()[:MAX_TAG_LENGTH]
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags[:MAX_TAGS]
