"""Notified publishing: at the scheduled time, tell a person to post it by hand.

Some formats can only be published from the platform's own app — a Story with
music, a poll or stickers has no API. For those the composer stores
``platform_extra["delivery"] = "notify"``. When the row comes due the publish
engine calls :func:`deliver_by_notification` instead of the provider: it sends
an in-app notification and an e-mail whose button opens the manual publish page
(media to download, caption to copy), and the row is settled as published with
``platform_extra["notified_at"]`` so the UI can say "Notified" instead of
"Published". Nothing is sent to the platform, so there is no platform post id
and no metrics to collect.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

NOTIFY_DELIVERY = "notify"


def is_notify_delivery(platform_extra: dict | None) -> bool:
    """Whether a PlatformPost is set to be published by hand after a notification."""
    return (platform_extra or {}).get("delivery") == NOTIFY_DELIVERY


def manual_publish_url(platform_post) -> str:
    """Absolute URL of the page the notification links to."""
    path = reverse(
        "composer:manual_publish",
        kwargs={
            "workspace_id": platform_post.post.workspace_id,
            "post_id": platform_post.post_id,
            "platform_post_id": platform_post.id,
        },
    )
    return f"{settings.APP_URL.rstrip('/')}{path}"


def _recipients(platform_post) -> list:
    """The post's author; without one, every workspace member who can create posts."""
    author = platform_post.post.author
    if author and author.is_active:
        return [author]
    from apps.members.models import WorkspaceMembership

    memberships = WorkspaceMembership.objects.filter(
        workspace_id=platform_post.post.workspace_id, user__is_active=True
    ).select_related("user", "custom_role")
    return [m.user for m in memberships if m.effective_permissions.get("create_posts")]


def deliver_by_notification(platform_post) -> dict:
    """Notify the people who will publish *platform_post* by hand.

    Returns the engine's success result. Raises if no one could be notified, so
    the engine retries instead of settling a post nobody heard about.
    """
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    recipients = _recipients(platform_post)
    if not recipients:
        raise RuntimeError("No one in the workspace can be notified to publish this post")

    account = platform_post.social_account
    url = manual_publish_url(platform_post)
    for user in recipients:
        # The worker has no request (and users have no stored language), so the
        # notification is written in the site's default language.
        with override(settings.LANGUAGE_CODE):
            notify(
                user,
                EventType.MANUAL_PUBLISH_DUE,
                title=_("Time to publish on %(account)s") % {"account": account.account_name},
                body=_(
                    "This post is set to be published by hand. Open it on your phone to download "
                    "the media and copy the caption, then publish it in the %(platform)s app."
                )
                % {"platform": account.get_platform_display()},
                data={
                    "action_url": url,
                    "post_id": str(platform_post.post_id),
                    "platform_post_id": str(platform_post.id),
                    "platform": account.platform,
                },
            )

    return {
        "success": True,
        "platform_post_id": "",
        "status_code": 200,
        "response": {"notified_at": timezone.now().isoformat()},
    }
