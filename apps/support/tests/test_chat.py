"""The live chat: the queue, one agent per chat, what the visitor is (not) shown,
and that no question is lost when nobody answers."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.support import services
from apps.support.models import Chat, ChatPageVisit, Ticket

CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"


@pytest.fixture
def online(agent):
    services.mark_presence(agent)
    return agent


def _write(client, body="Preciso de ajuda", path="/workspace/x/calendar/"):
    return client.post(
        reverse("support:chat_send"),
        {"body": body, "path": path, "title": "Calendário", "screen": "412x915", "language": "pt-BR"},
        HTTP_USER_AGENT=CHROME_ANDROID,
    )


@pytest.mark.django_db
def test_with_the_team_online_a_message_joins_the_queue_with_the_visitors_device(client, ana, online):
    client.force_login(ana)

    state = _write(client).json()

    chat = Chat.objects.get()
    assert (chat.visitor, chat.status) == (ana, "queued")
    assert (chat.browser, chat.operating_system, chat.device, chat.screen) == (
        "Chrome 131",
        "Android",
        "mobile",
        "412x915",
    )
    assert chat.current_path == "/workspace/x/calendar/"
    assert state["chat"]["position"] == 1 and state["messages"][0]["body"] == "Preciso de ajuda"


@pytest.mark.django_db(transaction=True)
def test_with_nobody_online_the_message_becomes_a_ticket(client, ana):
    client.force_login(ana)

    state = _write(client).json()

    ticket = Ticket.objects.get()
    assert not Chat.objects.exists()
    assert (ticket.requester, ticket.source, ticket.subject) == (ana, "chat", "Preciso de ajuda")
    assert state["ticket"]["number"] == ticket.pk


@pytest.mark.django_db
def test_the_visitor_is_told_their_place_in_the_queue(ana, bruno, online):
    first = services.visitor_writes(ana, "oi")
    second = services.visitor_writes(bruno, "olá")

    assert services.queue_position(first) == 1
    assert services.queue_position(second) == 2
    services.accept(first.pk, online)
    second.refresh_from_db()
    assert services.queue_position(second) == 1


@pytest.mark.django_db
def test_only_one_agent_takes_a_chat(ana, online, other_agent):
    chat = services.visitor_writes(ana, "oi")

    assert services.accept(chat.pk, online) is not None
    assert services.accept(chat.pk, other_agent) is None
    chat.refresh_from_db()
    assert chat.agent == online
    with pytest.raises(services.SupportError):
        services.agent_writes(chat, other_agent, "eu respondo")


@pytest.mark.django_db
def test_the_visitor_never_sees_their_device_nor_their_pages(client, ana, online):
    client.force_login(ana)
    _write(client)
    client.post(reverse("support:chat_visit"), {"path": "/workspace/x/inbox/", "title": "Caixa de entrada"})

    state = client.get(reverse("support:chat_state")).json()

    text = str(state)
    assert "Chrome" not in text and "Android" not in text and "/workspace/" not in text
    assert set(state["chat"]) == {"status", "position", "agent", "unread", "typing"}
    # The team sees both pages, newest first.
    team = services.console_chat(Chat.objects.get(), online)
    assert [v["path"] for v in team["visits"]] == ["/workspace/x/inbox/", "/workspace/x/calendar/"]
    assert team["page"]["title"] == "Caixa de entrada"


@pytest.mark.django_db
def test_a_reload_of_the_same_page_is_not_a_new_visit(ana, online):
    chat = services.visitor_writes(ana, "oi", page={"path": "/a/"})

    assert services.record_visit(chat, "/a/") is False
    assert services.record_visit(chat, "https://evil.example/") is False
    assert services.record_visit(chat, "/b/") is True
    assert ChatPageVisit.objects.filter(chat=chat).count() == 2


@pytest.mark.django_db
def test_each_visitor_reads_only_their_own_chat(client, ana, bruno, online):
    chat = services.accept(services.visitor_writes(ana, "segredo da Ana").pk, online)
    services.agent_writes(chat, online, "Olá Ana")
    client.force_login(bruno)

    state = client.get(reverse("support:chat_state")).json()

    assert state["chat"] is None and state["messages"] == []
    client.post(reverse("support:chat_close"))
    chat.refresh_from_db()
    assert chat.status == "active"


@pytest.mark.django_db
def test_turning_a_chat_into_a_ticket_keeps_the_conversation_and_tells_the_visitor(client, ana, online):
    chat = services.accept(services.visitor_writes(ana, "Não consigo conectar o Instagram").pk, online)
    services.agent_writes(chat, online, "Vou verificar com a Meta")

    ticket = services.convert_to_ticket(chat, agent=online, note="Checar token")

    assert (ticket.requester, ticket.assignee, ticket.status, ticket.source) == (ana, online, "in_progress", "chat")
    assert ticket.subject == "Não consigo conectar o Instagram"
    assert list(ticket.messages.filter(is_internal=False).values_list("body", flat=True)) == [
        "Não consigo conectar o Instagram",
        "Vou verificar com a Meta",
    ]
    assert ticket.messages.get(is_internal=True).body == "Checar token"
    chat.refresh_from_db()
    assert chat.status == "closed"
    # The visitor is told once, with the ticket's link.
    client.force_login(ana)
    ended = client.get(reverse("support:chat_state")).json()["ended"]
    assert ended["ticket"]["number"] == ticket.pk
    assert client.get(reverse("support:chat_state")).json()["ended"] is None


@pytest.mark.django_db(transaction=True)
def test_a_queued_chat_whose_visitor_left_becomes_a_ticket(ana, bruno, online):
    gone = services.visitor_writes(ana, "Alguém aí?")
    waiting = services.visitor_writes(bruno, "Oi")
    Chat.objects.filter(pk=gone.pk).update(visitor_seen_at=timezone.now() - timedelta(minutes=11))

    assert services.convert_abandoned() == 1

    gone.refresh_from_db()
    assert gone.status == "closed" and gone.ticket.requester == ana and gone.ticket.status == "open"
    assert Chat.objects.get(pk=waiting.pk).status == "queued"


@pytest.mark.django_db
def test_the_console_marks_the_agent_online_and_lists_the_queue(client, ana, agent):
    assert not services.agents_online()
    client.force_login(agent)

    assert client.get(reverse("support_staff:chat")).status_code == 200
    assert services.agents_online()
    services.visitor_writes(ana, "oi")
    feed = client.get(reverse("support_staff:chat_feed")).json()
    assert [item["visitor"] for item in feed["queue"]] == [ana.display_name]
    assert client.get(reverse("support_staff:chat_pulse")).json() == {"queued": 1, "unread": 0}


@pytest.mark.django_db
def test_the_agent_sees_what_they_have_not_read(ana, online):
    chat = services.visitor_writes(ana, "oi")
    services.accept(chat.pk, online)
    services.visitor_writes(ana, "tem alguém?")

    assert services.pulse(online)["unread"] == 1
    assert services.console_feed(online)["mine"][0]["unread"] == 1
    services.console_chat(Chat.objects.get(pk=chat.pk), online)
    assert services.pulse(online)["unread"] == 0


@pytest.mark.django_db
def test_a_person_has_one_open_chat(ana, online):
    first = services.visitor_writes(ana, "oi")
    second = services.visitor_writes(ana, "de novo")

    assert first.pk == second.pk and first.messages.count() == 2


@pytest.mark.django_db
def test_the_widget_is_drawn_only_for_a_customer_in_their_own_account(client, ana, agent):
    client.force_login(ana)
    assert b'x-data="supportChat"' in client.get(reverse("support:help")).content

    client.force_login(agent)
    assert b"supportChat" not in client.get(reverse("support_staff:tickets")).content

    client.post(reverse("accounts:start_impersonation", kwargs={"user_id": ana.pk}))
    assert b'x-data="supportChat"' not in client.get(reverse("support:help")).content


@pytest.mark.django_db
def test_a_customer_reads_only_their_own_past_chats_without_the_teams_details(client, ana, bruno, online):
    chat = services.accept(services.visitor_writes(ana, "Pergunta da Ana", user_agent=CHROME_ANDROID).pk, online)
    services.agent_writes(chat, online, "Resposta da equipe")
    services.record_visit(chat, "/workspace/x/inbox/", "Caixa de entrada")
    services.agent_closes(chat)
    client.force_login(ana)

    listing = client.get(reverse("support:chats")).content.decode()
    page = client.get(reverse("support:chat", args=[chat.pk])).content.decode()

    assert "Pergunta da Ana" in listing
    assert "Pergunta da Ana" in page and "Resposta da equipe" in page
    assert "Chrome" not in page and "/workspace/x/inbox/" not in page
    client.force_login(bruno)
    assert client.get(reverse("support:chat", args=[chat.pk])).status_code == 404
    assert "Pergunta da Ana" not in client.get(reverse("support:chats")).content.decode()


@pytest.mark.django_db
def test_the_team_reads_every_past_chat_with_the_visit(client, ana, online):
    chat = services.accept(services.visitor_writes(ana, "Pergunta da Ana", user_agent=CHROME_ANDROID).pk, online)
    services.record_visit(chat, "/workspace/x/inbox/", "Caixa de entrada - Connekt Flow")
    services.agent_closes(chat)
    client.force_login(online)

    listing = client.get(reverse("support_staff:chat_history"), {"q": "ana@"}).content.decode()
    page = client.get(reverse("support_staff:chat_history_detail", args=[chat.pk])).content.decode()

    assert "Pergunta da Ana" in listing
    assert "Chrome 131" in page and "/workspace/x/inbox/" in page and "Caixa de entrada" in page
    assert "Pergunta da Ana" not in client.get(reverse("support_staff:chat_history"), {"q": "bruno"}).content.decode()


@pytest.mark.django_db
def test_each_side_sees_the_other_typing_until_they_send(client, ana, online, other_agent):
    chat = services.accept(services.visitor_writes(ana, "oi").pk, online)
    client.force_login(ana)

    client.post(reverse("support:chat_typing"))
    assert services.console_chat(Chat.objects.get(pk=chat.pk), online)["typing"] is True
    assert services.console_feed(online)["mine"][0]["typing"] is True
    services.visitor_writes(ana, "pronto")
    assert services.console_chat(Chat.objects.get(pk=chat.pk), online)["typing"] is False

    # Only the agent answering the chat can show as typing in it.
    services.agent_types(Chat.objects.get(pk=chat.pk), other_agent)
    assert client.get(reverse("support:chat_state")).json()["chat"]["typing"] is False
    client.force_login(online)
    client.post(reverse("support_staff:chat_typing", args=[chat.pk]))
    client.force_login(ana)
    assert client.get(reverse("support:chat_state")).json()["chat"]["typing"] is True
    services.agent_writes(Chat.objects.get(pk=chat.pk), online, "Olá")
    assert client.get(reverse("support:chat_state")).json()["chat"]["typing"] is False


@pytest.mark.django_db
def test_typing_fades_after_a_few_seconds(ana, online):
    chat = services.accept(services.visitor_writes(ana, "oi").pk, online)
    Chat.objects.filter(pk=chat.pk).update(visitor_typing_at=timezone.now() - timedelta(seconds=6))

    assert services.console_chat(Chat.objects.get(pk=chat.pk), online)["typing"] is False


@pytest.mark.django_db
def test_a_visitor_cannot_mark_typing_in_someone_elses_chat(client, ana, bruno, online):
    chat = services.accept(services.visitor_writes(ana, "oi").pk, online)
    client.force_login(bruno)

    client.post(reverse("support:chat_typing"))

    assert Chat.objects.get(pk=chat.pk).visitor_typing_at is None
