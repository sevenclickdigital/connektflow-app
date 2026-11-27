"""Custom managers for media library models."""

from django.db import connection, models
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce


class MediaAssetManager(models.Manager):
    def for_workspace(self, workspace_id):
        return self.get_queryset().filter(workspace_id=workspace_id)

    def for_org(self, organization_id):
        return self.get_queryset().filter(organization_id=organization_id)

    def for_workspace_with_shared(self, workspace_id, organization_id):
        """Return workspace-scoped assets plus shared org-level assets."""
        return self.get_queryset().filter(
            Q(workspace_id=workspace_id) | Q(workspace__isnull=True, organization_id=organization_id)
        )

    def shared_only(self, organization_id):
        """Return only shared org-level assets (workspace is null)."""
        return self.get_queryset().filter(
            organization_id=organization_id,
            workspace__isnull=True,
        )

    def originals(self, queryset, visible):
        """The originals in ``queryset``, each with ``version_count``.

        A library lists one card per original; its crops and edits are its
        versions (``MediaAsset.original``). ``version_count`` counts only the
        copies in ``visible`` (the viewer's scope): a shared photo cropped in
        another workspace does not count here.
        """
        copies = (
            visible.filter(original=OuterRef("pk")).order_by().values("original").annotate(n=Count("pk")).values("n")
        )
        return queryset.filter(original__isnull=True).annotate(
            version_count=Coalesce(Subquery(copies, output_field=IntegerField()), 0)
        )

    def version_group(self, asset, visible):
        """``asset``'s original and its copies within ``visible``, oldest first."""
        root = asset.original_id_or_self
        return visible.filter(Q(pk=root) | Q(original_id=root)).order_by("created_at")

    def with_last_used_at(self, queryset=None):
        """Annotate each asset with ``last_used_at``.

        ``PostMedia`` itself has no ``created_at``, so we walk through to
        the parent ``Post.created_at`` (verified at composer/models.py:262).
        Returns ``NULL`` for assets that have never been attached to a Post.

        The related_name on ``PostMedia.media_asset`` is ``post_usages``;
        see [composer/models.py:534-538].
        """
        qs = queryset if queryset is not None else self.get_queryset()
        return qs.annotate(last_used_at=Max("post_usages__post__created_at"))

    def search(self, query, queryset=None):
        """Full-text search on filename and tags.

        Uses PostgreSQL full-text search when available, falls back to
        case-insensitive LIKE queries on SQLite.
        """
        qs = queryset if queryset is not None else self.get_queryset()
        if not query:
            return qs

        if connection.vendor == "postgresql":
            from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

            search_vector = SearchVector("filename", weight="A")
            search_query = SearchQuery(query, search_type="websearch")

            qs = (
                qs.annotate(
                    search=search_vector,
                    rank=SearchRank(search_vector, search_query),
                )
                .filter(Q(search=search_query) | Q(tags__contains=[query]))
                .order_by("-rank")
            )
        else:
            # SQLite fallback: simple case-insensitive contains
            qs = qs.filter(Q(filename__icontains=query) | Q(tags__icontains=query))

        return qs
