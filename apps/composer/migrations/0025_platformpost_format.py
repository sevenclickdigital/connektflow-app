from django.db import migrations, models

# The format a row used to carry as a ``platform_extra["post_type"]`` hint, and
# the media shape it needs. A hint the attachments no longer fit stays a
# default row: the publisher already ignored it, and turning it into a format
# now would make the post fail where it used to publish.
HINT_FITS = {
    "story": lambda count, first: count == 1 and first in ("image", "video"),
    "reel": lambda count, first: count == 1 and first == "video",
    "profile_photo": lambda count, first: count == 1 and first == "image",
}


def backfill_format(apps, schema_editor):
    PlatformPost = apps.get_model("composer", "PlatformPost")
    PostMedia = apps.get_model("composer", "PostMedia")

    rows = PlatformPost.objects.filter(platform_extra__post_type__in=list(HINT_FITS)).only(
        "id", "post_id", "platform_extra"
    )
    for row in rows.iterator(chunk_size=500):
        media = list(
            PostMedia.objects.filter(post_id=row.post_id)
            .exclude(media_asset__file="")
            .order_by("position")
            .values_list("media_asset__media_type", flat=True)[:2]
        )
        hint = row.platform_extra["post_type"]
        if HINT_FITS[hint](len(media), media[0] if media else None):
            PlatformPost.objects.filter(pk=row.pk).update(format=hint)


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0024_tag_kind"),
        ("social_accounts", "0027_x_platform_choice"),
    ]

    operations = [
        # db_default keeps INSERTs from the release still running during the
        # deploy valid: they don't name the column.
        migrations.AddField(
            model_name="platformpost",
            name="format",
            field=models.CharField(
                choices=[
                    ("default", "Post"),
                    ("story", "Story"),
                    ("reel", "Reels"),
                    ("profile_photo", "Photo"),
                ],
                db_default="default",
                default="default",
                max_length=20,
            ),
        ),
        # The constraint change lives in 0026: an ALTER TABLE in the same
        # transaction as these UPDATEs can be refused while their deferred FK
        # trigger events are pending.
        migrations.RunPython(backfill_format, migrations.RunPython.noop, elidable=True),
    ]
