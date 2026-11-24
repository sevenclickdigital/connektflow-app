from django.contrib import admin

from .models import MagicLinkToken


@admin.register(MagicLinkToken)
class MagicLinkTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "created_at", "expires_at", "is_consumed")
    list_filter = ("is_consumed", "created_at")
    search_fields = ("user__email", "workspace__name")
    # Neither the token nor its hash: the raw token only exists in the e-mail,
    # and nothing here should help anyone reconstruct or replay a link.
    exclude = ("token", "token_hash")
    readonly_fields = ("id", "created_at", "last_used_at")
