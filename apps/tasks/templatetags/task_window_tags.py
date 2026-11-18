"""``{% task_window_host %}``: the board's new task window on any other page.

Include it once in a page that sends something to the task board (the idea hub, the
calendar, the Radar); a button opens the window with
``$dispatch('open-task-window', { url: '<tasks:task_window URL with the source>' })``.
Renders nothing for whoever can't create tasks (the client, a member without
``create_posts``).
"""

from django import template
from django.urls import reverse

from apps.members.models import WorkspaceMembership

register = template.Library()


@register.inclusion_tag("tasks/partials/_task_window_host.html", takes_context=True)
def task_window_host(context, own_picker=True, own_tags=True):
    """``own_picker=False`` when the page has its own media library window, which then
    opens on "open-reference-picker"; ``own_tags=False`` when it already has the tag groups
    window and "Manage tags" (composer/partials/_tag_groups.html, _tag_manager.html)."""
    request = context["request"]
    workspace = getattr(request, "workspace", None)
    membership = getattr(request, "workspace_membership", None)
    allowed = (
        workspace is not None
        and membership is not None
        and membership.workspace_role != WorkspaceMembership.WorkspaceRole.CLIENT
        and bool(membership.effective_permissions.get("create_posts"))
    )
    if not allowed:
        return {"allowed": False}
    from apps.composer.services import tag_groups_payload
    from apps.tasks.services import media_folder

    picker_url = ""
    if own_picker:
        folder = media_folder(workspace)
        picker_url = (
            f"{reverse('composer:idea_media_picker', kwargs={'workspace_id': workspace.id})}?folder={folder.id}"
        )
    return {
        "allowed": True,
        "request": request,
        "workspace": workspace,
        "own_picker": own_picker,
        "own_tags": own_tags,
        "picker_open_url": picker_url,
        "tag_groups": tag_groups_payload(workspace.id) if own_tags else [],
    }
