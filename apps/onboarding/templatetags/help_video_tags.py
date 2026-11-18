from django import template

from apps.onboarding.help_videos import video_for_page

register = template.Library()


@register.simple_tag
def page_help_video(page):
    """``{% page_help_video tab as help_video %}``: the page's "See how it
    works" video ({"embed_url", "video_url"}) or None. One query per banner."""
    return video_for_page(page)
