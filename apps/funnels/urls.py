from django.urls import path

from . import views

app_name = "funnels"

urlpatterns = [
    path("", views.funnel_inbox, name="inbox"),
    path("board/", views.board, name="board"),
    path("board/move/", views.board_move, name="board_move"),
    path("settings/", views.funnel_settings, name="settings"),
    path("settings/funnel/", views.funnel_save, name="funnel_save"),
    path("settings/funnel/<uuid:funnel_id>/delete/", views.funnel_delete, name="funnel_delete"),
    path("settings/funnel/<uuid:funnel_id>/stage/", views.stage_save, name="stage_save"),
    path("settings/stage/<uuid:stage_id>/delete/", views.stage_delete, name="stage_delete"),
    path("settings/stage/<uuid:stage_id>/move/", views.stage_reorder, name="stage_reorder"),
    path("message/<uuid:message_id>/", views.opportunity_update, name="opportunity_update"),
    path("message/<uuid:message_id>/next/", views.opportunity_next, name="opportunity_next"),
    path("message/<uuid:message_id>/flow/", views.flow_start, name="flow_start"),
]
