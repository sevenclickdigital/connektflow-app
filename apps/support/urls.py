"""The customer's support pages and the chat widget's calls, under ``/support/``.

Their own namespace, outside ``OPERATOR_NAMESPACES``: a team account is sent
back to the administrative area (``OperatorAreaMiddleware``).
"""

from django.urls import path

from . import views

app_name = "support"

urlpatterns = [
    path("", views.help_center, name="help"),
    path("tutorials/", views.tutorial_list, {"kind": "videos"}, name="tutorials"),
    path("guides/", views.tutorial_list, {"kind": "guides"}, name="guides"),
    path("tutorials/<slug:page>/", views.tutorial, name="tutorial"),
    path("tickets/", views.ticket_list, name="tickets"),
    path("tickets/new/", views.ticket_new, name="ticket_new"),
    path("tickets/<int:ticket_id>/", views.ticket_detail, name="ticket"),
    path("tickets/<int:ticket_id>/reply/", views.ticket_reply, name="ticket_reply"),
    path("tickets/<int:ticket_id>/attachments/<int:message_id>/", views.ticket_attachment, name="ticket_attachment"),
    path("conversations/", views.chat_history, name="chats"),
    path("conversations/<uuid:chat_id>/", views.chat_history_detail, name="chat"),
    path("chat/", views.chat_state, name="chat_state"),
    path("chat/send/", views.chat_send, name="chat_send"),
    path("chat/visit/", views.chat_visit, name="chat_visit"),
    path("chat/close/", views.chat_close, name="chat_close"),
    path("chat/typing/", views.chat_typing, name="chat_typing"),
]
