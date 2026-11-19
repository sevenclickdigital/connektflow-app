"""Registry of opportunity sources shown in Platforms and in the Radar.

Adding a source means: a ``OpportunityConnection.Source`` choice, a client in
``providers/``, a connect view and a Radar screen, and one entry here. The
Platforms grid, the connected-accounts list and the Radar index all render
from this registry, so nothing else has to learn about the new source.

``UPCOMING`` lists sources announced but not built: they show as "Soon" cards
with no connect or open action, and move to ``SOURCES`` once they exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from .models import OpportunityConnection


@dataclass(frozen=True)
class OpportunitySource:
    key: str
    label: str | Promise
    description: str | Promise
    connect_url_name: str = ""
    open_url_name: str = ""


SOURCES: dict[str, OpportunitySource] = {
    OpportunityConnection.Source.SHOPEE_AFFILIATE: OpportunitySource(
        key=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        label=_("Shopee Affiliates"),
        description=_("Products with commission to share"),
        connect_url_name="radar:connect_shopee",
        open_url_name="radar:shopee",
    ),
}

UPCOMING: tuple[OpportunitySource, ...] = (
    OpportunitySource(key="shein", label="Shein", description=_("Fashion and accessories to share")),
    OpportunitySource(key="mercado_livre", label="Mercado Livre", description=_("Marketplace offers to share")),
    OpportunitySource(key="tiktok_shop", label="TikTok Shop", description=_("Shop products to promote")),
)


@dataclass(frozen=True)
class SourceCard:
    """A source as one workspace sees it: whether it is linked, and where to go."""

    source: OpportunitySource
    connection: OpportunityConnection | None
    connect_url: str
    open_url: str
    upcoming: bool = False


def all_sources() -> list[OpportunitySource]:
    """Every source, built and announced, in the order the grids show them."""
    return [*SOURCES.values(), *UPCOMING]


def source_cards(workspace_id) -> list[SourceCard]:
    """The workspace's source cards. A source the operator hid (the administrative
    area's Platforms screen, ``PlatformVisibility``) is left out unless this
    workspace already linked it, so an existing connection stays reachable."""
    from apps.social_accounts.models import PlatformVisibility

    hidden = PlatformVisibility.hidden_keys()
    connections = {c.source: c for c in OpportunityConnection.objects.for_workspace(workspace_id)}
    cards = [
        SourceCard(
            source=source,
            connection=connections.get(key),
            connect_url=reverse(source.connect_url_name, kwargs={"workspace_id": workspace_id}),
            open_url=reverse(source.open_url_name, kwargs={"workspace_id": workspace_id}),
        )
        for key, source in SOURCES.items()
    ] + [SourceCard(source=source, connection=None, connect_url="", open_url="", upcoming=True) for source in UPCOMING]
    return [card for card in cards if card.connection or card.source.key not in hidden]
