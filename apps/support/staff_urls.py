"""The team's support screens, under ``/staff/support/``. Access is enforced in the views (``staff_required``)."""

from django.urls import path

from . import staff_views

app_name = "support_staff"

urlpatterns = [
    path("", staff_views.ticket_queue, name="tickets"),
    path("tickets/<int:ticket_id>/", staff_views.ticket_detail, name="ticket"),
    path("tickets/<int:ticket_id>/reply/", staff_views.ticket_reply, name="ticket_reply"),
    path("tickets/<int:ticket_id>/update/", staff_views.ticket_update, name="ticket_update"),
    path(
        "tickets/<int:ticket_id>/attachments/<int:message_id>/",
        staff_views.ticket_attachment,
        name="ticket_attachment",
    ),
    path("faq/", staff_views.faq_list, name="faq"),
    path("faq/new/", staff_views.faq_edit, name="faq_new"),
    path("faq/<int:entry_id>/", staff_views.faq_edit, name="faq_edit"),
    path("faq/<int:entry_id>/delete/", staff_views.faq_delete, name="faq_delete"),
    path("chat/", staff_views.chat_console, name="chat"),
    path("chat/history/", staff_views.chat_history, name="chat_history"),
    path("chat/history/<uuid:chat_id>/", staff_views.chat_history_detail, name="chat_history_detail"),
    path("chat/feed/", staff_views.chat_feed, name="chat_feed"),
    path("chat/pulse/", staff_views.chat_pulse, name="chat_pulse"),
    path("chat/<uuid:chat_id>/", staff_views.chat_detail, name="chat_detail"),
    path("chat/<uuid:chat_id>/accept/", staff_views.chat_accept, name="chat_accept"),
    path("chat/<uuid:chat_id>/send/", staff_views.chat_send, name="chat_send"),
    path("chat/<uuid:chat_id>/close/", staff_views.chat_close, name="chat_close"),
    path("chat/<uuid:chat_id>/typing/", staff_views.chat_typing, name="chat_typing"),
    path("chat/<uuid:chat_id>/convert/", staff_views.chat_convert, name="chat_convert"),
]
