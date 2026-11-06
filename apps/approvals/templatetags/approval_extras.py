"""Template helpers for the approval-workflow UI."""

from django import template
from django.utils.translation import gettext_lazy as _

register = template.Library()

# Presentation metadata per editorial status — mirrors the approved design's
# ``STATUS_META`` (dot / background / foreground / border + label). Kept here so
# every approval surface (publish tab, composer panel, client portal) renders an
# identical badge from one source.
STATUS_META = {
    "draft": {"label": _("Draft"), "dot": "#A8A29E", "bg": "#F5F5F4", "fg": "#57534E", "bd": "#E7E5E4"},
    "pending_review": {"label": _("Pending Review"), "dot": "#F97316", "bg": "#FFF7ED", "fg": "#C2410C", "bd": "#FED7AA"},
    "pending_client": {"label": _("Pending Client"), "dot": "#F97316", "bg": "#FFF7ED", "fg": "#C2410C", "bd": "#FED7AA"},
    "approved": {"label": _("Approved"), "dot": "#14B8A6", "bg": "#F0FDFA", "fg": "#0F766E", "bd": "#99F6E4"},
    "changes_requested": {
        "label": _("Changes Requested"),
        "dot": "#F97316",
        "bg": "#FFF7ED",
        "fg": "#C2410C",
        "bd": "#FED7AA",
    },
    "rejected": {"label": _("Rejected"), "dot": "#EF4444", "bg": "#FEF2F2", "fg": "#B91C1C", "bd": "#FECACA"},
    "scheduled": {"label": _("Scheduled"), "dot": "#3B82F6", "bg": "#EFF6FF", "fg": "#1D4ED8", "bd": "#BFDBFE"},
    "publishing": {"label": _("Publishing"), "dot": "#6366F1", "bg": "#EEF2FF", "fg": "#4338CA", "bd": "#C7D2FE"},
    "published": {"label": _("Published"), "dot": "#22C55E", "bg": "#F0FDF4", "fg": "#15803D", "bd": "#BBF7D0"},
    "partially_published": {
        "label": _("Partially Published"),
        "dot": "#F59E0B",
        "bg": "#FFFBEB",
        "fg": "#B45309",
        "bd": "#FDE68A",
    },
    "failed": {"label": _("Failed"), "dot": "#EF4444", "bg": "#FEF2F2", "fg": "#B91C1C", "bd": "#FECACA"},
    "on_hold": {"label": _("Hold Requested"), "dot": "#7C3AED", "bg": "#F5F3FF", "fg": "#6D28D9", "bd": "#DDD6FE"},
}


@register.inclusion_tag("approvals/partials/_status_badge.html")
def status_badge(status):
    """Render the editorial-status pill (dot + label) for *status*."""
    return {"meta": STATUS_META.get(status, STATUS_META["draft"])}
