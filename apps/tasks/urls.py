from django.urls import path

from . import views

app_name = "tasks"

urlpatterns = [
    path("", views.board, name="board"),
    path("create/", views.task_create, name="task_create"),
    path("window/", views.new_task_window, name="new_task_window"),
    path("art-settings/", views.art_settings, name="art_settings"),
    path("<uuid:task_id>/", views.task_detail, name="task_detail"),
    path("<uuid:task_id>/update/", views.task_update, name="task_update"),
    path("<uuid:task_id>/move/", views.task_move, name="task_move"),
    path("<uuid:task_id>/delete/", views.task_delete, name="task_delete"),
    path("<uuid:task_id>/stage/", views.task_stage, name="task_stage"),
    path("<uuid:task_id>/create-post/", views.task_create_post, name="task_create_post"),
    path("<uuid:task_id>/items/add/", views.item_add, name="item_add"),
    path("<uuid:task_id>/checklists/add/", views.checklist_add, name="checklist_add"),
    path("<uuid:task_id>/checklists/<uuid:checklist_id>/update/", views.checklist_update, name="checklist_update"),
    path("<uuid:task_id>/checklists/<uuid:checklist_id>/delete/", views.checklist_delete, name="checklist_delete"),
    path("<uuid:task_id>/checklists/<uuid:checklist_id>/comment/", views.checklist_comment, name="checklist_comment"),
    path("<uuid:task_id>/items/<uuid:item_id>/deliver/", views.item_deliver, name="item_deliver"),
    path("<uuid:task_id>/items/<uuid:item_id>/art/remove/", views.item_art_remove, name="item_art_remove"),
    path("<uuid:task_id>/items/<uuid:item_id>/art/crop/", views.item_art_crop, name="item_art_crop"),
    path("<uuid:task_id>/items/<uuid:item_id>/toggle/", views.item_toggle, name="item_toggle"),
    path("<uuid:task_id>/items/<uuid:item_id>/settings/", views.item_settings, name="item_settings"),
    path("<uuid:task_id>/items/<uuid:item_id>/count/", views.item_count, name="item_count"),
    path("<uuid:task_id>/items/<uuid:item_id>/delete/", views.item_delete, name="item_delete"),
    path("columns/create/", views.column_create, name="column_create"),
    path("columns/reorder/", views.column_reorder, name="column_reorder"),
    path("columns/<uuid:column_id>/rename/", views.column_rename, name="column_rename"),
    path("columns/<uuid:column_id>/delete/", views.column_delete, name="column_delete"),
]
