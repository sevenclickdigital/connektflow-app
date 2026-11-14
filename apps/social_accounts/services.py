"""Account operations shared by the screens and the platform callbacks."""

from django.db.models import Count

from apps.composer.models import PlatformPost, Post


def remove_account_data(account) -> None:
    """Delete a connection and everything stored for it.

    Posts that target only this account go with it, since nothing else keeps
    them alive; a post shared with other accounts keeps its other targets and
    loses this one through the cascade. Messages, metrics and tokens hang off
    the account and are removed by its own cascade.

    Talks to no platform: unsubscribing webhooks and revoking the grant need a
    working token and are the caller's business.
    """
    orphan_post_ids = list(
        PlatformPost.objects.filter(social_account=account)
        .values("post_id")
        # Distinct accounts, not rows: a post's Feed and Story on this one
        # account are two rows and still nothing else to keep it alive.
        .annotate(total_platforms=Count("post__platform_posts__social_account", distinct=True))
        .filter(total_platforms=1)
        .values_list("post_id", flat=True)
    )
    if orphan_post_ids:
        Post.objects.filter(id__in=orphan_post_ids).delete()
    account.delete()
