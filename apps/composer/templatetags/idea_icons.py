"""Template access to the idea icon library (``apps.composer.idea_icons``)."""

from django import template

from apps.composer import idea_icons

register = template.Library()


@register.simple_tag
def idea_icon_markup(slug):
    """The inner markup of an idea icon's ``<svg>`` (the default for none)."""
    return idea_icons.icon_markup(slug)


@register.simple_tag
def category_icon_markup(category):
    """The inner markup of the icon of a template category (or ``all``)."""
    return idea_icons.icon_markup(idea_icons.category_style(category)[0])


@register.filter
def category_tone(category):
    """The tone of a template category, for an ``icon-tone-*`` class."""
    return idea_icons.category_style(category)[1]
