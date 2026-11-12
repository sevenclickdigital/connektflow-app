"""The PlatformPost ``format`` migrations, applied to a table that already has rows.

``0025`` adds the column and turns a ``platform_extra["post_type"]`` hint into
the row's format, but only where the media still fits it — elsewhere the
publisher already ignored the hint, and a format would make the post fail.
``0026`` swaps the (post, account) uniqueness for (post, account, format).
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

BEFORE = ("composer", "0024_tag_kind")
HEAD = ("composer", "0026_platformpost_unique_format")


def _migrate(target):
    MigrationExecutor(connection).migrate([target])


@pytest.fixture
def at_0024(db):
    _migrate(BEFORE)
    yield MigrationExecutor(connection).loader.project_state(BEFORE).apps
    _migrate(HEAD)


@pytest.mark.django_db(transaction=True)
def test_hints_become_formats_only_where_the_media_fits(at_0024, user, organization):
    old_platform_post = at_0024.get_model("composer", "PlatformPost")
    workspace = Workspace.objects.create(organization=organization, name="WS")
    accounts = {}

    def account(platform):
        if platform not in accounts:
            accounts[platform] = SocialAccount.objects.create(
                workspace=workspace,
                platform=platform,
                account_platform_id=f"{platform}-1",
                account_name=platform,
                connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            )
        return accounts[platform]

    def row(platform, extra, media=(), status="draft"):
        post = Post.objects.create(workspace=workspace, author=user, caption="x")
        for position, media_type in enumerate(media):
            asset = MediaAsset.objects.create(
                organization=organization,
                workspace=workspace,
                uploaded_by=user,
                file=f"test/{post.pk}-{position}",
                filename="f",
                media_type=media_type,
                mime_type="image/jpeg",
            )
            PostMedia.objects.create(post=post, media_asset=asset, position=position)
        pp = old_platform_post.objects.create(
            post_id=post.pk, social_account_id=account(platform).pk, platform_extra=extra, status=status
        )
        return pp.pk

    seeded = {
        row("instagram", {"post_type": "story"}, ["image"]): "story",
        row("facebook", {"post_type": "story"}, ["image", "image"]): "default",
        row("facebook", {"post_type": "reel"}, ["video"]): "reel",
        row("instagram", {"post_type": "reel"}, ["image"]): "default",
        row("google_business", {"post_type": "profile_photo", "photo_category": "MENU"}, ["image"]): "profile_photo",
        row("linkedin", {"post_type": "text"}): "default",
        row("instagram", {"post_type": "story", "delivery": "notify"}, ["video"]): "story",
        row("facebook", {"post_type": "story"}, ["video"], status="published"): "story",
    }
    extras_before = dict(old_platform_post.objects.values_list("pk", "platform_extra"))

    _migrate(HEAD)

    after = {pp.pk: pp for pp in PlatformPost.objects.filter(pk__in=seeded)}
    assert {pk: after[pk].format for pk in seeded} == seeded
    assert {pk: after[pk].platform_extra for pk in seeded} == {pk: extras_before[pk] for pk in seeded}


@pytest.mark.django_db
def test_an_account_takes_one_row_per_format(user, organization):
    workspace = Workspace.objects.create(organization=organization, name="WS")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig",
        account_name="ig",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    post = Post.objects.create(workspace=workspace, author=user)
    PlatformPost.objects.create(post=post, social_account=account)
    PlatformPost.objects.create(post=post, social_account=account, format="story")

    with pytest.raises(IntegrityError), transaction.atomic():
        PlatformPost.objects.create(post=post, social_account=account, format="story")

    # The column has a database default, so an INSERT that doesn't name it
    # (the release still running while this one deploys) stays valid.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'composer_platform_post' AND column_name = 'format'"
        )
        assert "default" in cursor.fetchone()[0]
