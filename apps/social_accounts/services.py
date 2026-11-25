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


def reorder_accounts(workspace_id, ordered_ids) -> None:
    """Number the brand's accounts in ``ordered_ids`` order.

    Ids that are not this brand's accounts are ignored; the brand's accounts
    missing from the list keep their relative order after the ones listed.
    """
    from apps.social_accounts.models import SocialAccount

    wanted = {key: index for index, key in enumerate(dict.fromkeys(str(value) for value in ordered_ids))}
    accounts = list(SocialAccount.objects.for_workspace(workspace_id).in_display_order().only("id", "position"))
    accounts.sort(key=lambda account: wanted.get(str(account.id), len(wanted)))
    for position, account in enumerate(accounts, start=1):
        account.position = position
    SocialAccount.objects.bulk_update(accounts, ["position"])
