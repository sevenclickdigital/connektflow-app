"""Tickets: each customer reaches only their own, the team's notes never reach them,
and replies move the status and tell the other side."""

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.support import services
from apps.support.models import Ticket, TicketMessage

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _ticket(requester, **extra):
    return services.open_ticket(
        requester=requester, kind="bug", subject="Erro ao publicar", body="Aparece um erro", **extra
    )


@pytest.mark.django_db(transaction=True)
def test_opening_a_ticket_tells_the_team(client, ana, agent):
    client.force_login(ana)

    response = client.post(
        reverse("support:ticket_new"), {"kind": "bug", "subject": "Erro ao publicar", "body": "Aparece um erro"}
    )

    ticket = Ticket.objects.get()
    assert response.status_code == 302 and response["Location"] == reverse("support:ticket", args=[ticket.pk])
    assert (ticket.requester, ticket.kind, ticket.status) == (ana, "bug", "open")
    assert [m.to for m in mail.outbox] == [[agent.email]]
    assert f"#{ticket.pk}" in mail.outbox[0].subject


@pytest.mark.django_db
def test_a_customer_never_reaches_someone_elses_ticket(client, ana, bruno):
    ticket = _ticket(ana)
    client.force_login(bruno)

    assert client.get(reverse("support:ticket", args=[ticket.pk])).status_code == 404
    client.post(reverse("support:ticket_reply", args=[ticket.pk]), {"body": "intruso"})
    assert not ticket.messages.filter(body="intruso").exists()
    assert ticket.pk not in [t.pk for t in services.tickets_of(bruno)]
    assert client.get(reverse("support:tickets")).content.count(b"Erro ao publicar") == 0


@pytest.mark.django_db
def test_the_teams_notes_never_reach_the_customer(client, ana, agent, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    ticket = _ticket(ana)
    note = services.reply(
        ticket,
        author=agent,
        body="Cliente com cartão recusado",
        from_team=True,
        internal=True,
        attachment=SimpleUploadedFile("print.png", PNG, content_type="image/png"),
    )
    client.force_login(ana)

    page = client.get(reverse("support:ticket", args=[ticket.pk]))

    assert b"Cliente com cart" not in page.content
    assert client.get(reverse("support:ticket_attachment", args=[ticket.pk, note.pk])).status_code == 404
    # A note changes nothing the customer sees, and tells nobody.
    ticket.refresh_from_db()
    assert ticket.status == "open"


@pytest.mark.django_db(transaction=True)
def test_a_team_reply_waits_for_the_customer_and_emails_them(ana, agent):
    ticket = _ticket(ana)
    mail.outbox.clear()

    services.reply(ticket, author=agent, body="Pode tentar de novo?", from_team=True)

    ticket.refresh_from_db()
    assert (ticket.status, ticket.assignee) == ("waiting_customer", agent)
    assert [m.to for m in mail.outbox] == [[ana.email]]
    assert "Pode tentar de novo?" in mail.outbox[0].body
    assert services.has_news(services.tickets_of(ana).get())


@pytest.mark.django_db
def test_a_customer_reply_reopens_a_resolved_ticket(ana):
    ticket = _ticket(ana)
    services.set_status(ticket, Ticket.Status.RESOLVED)

    services.reply(ticket, author=ana, body="Voltou a acontecer", from_team=False)

    ticket.refresh_from_db()
    assert ticket.status == "open"


@pytest.mark.django_db
def test_only_images_are_attached(ana):
    with pytest.raises(services.SupportError):
        _ticket(ana, attachment=SimpleUploadedFile("page.html", b"<script>", content_type="image/png"))
    assert not Ticket.objects.exists()


@pytest.mark.django_db
def test_an_attachment_is_served_only_through_the_check(client, ana, bruno, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    ticket = _ticket(ana, attachment=SimpleUploadedFile("print.png", PNG, content_type="image/png"))
    message = ticket.messages.get()
    url = reverse("support:ticket_attachment", args=[ticket.pk, message.pk])
    assert message.attachment.name.startswith("support_attachments/")

    client.force_login(bruno)
    assert client.get(url).status_code == 404
    client.force_login(ana)
    response = client.get(url)
    assert response.status_code == 200 and response["X-Content-Type-Options"] == "nosniff"
    assert b"".join(response.streaming_content) == PNG


@pytest.mark.django_db
def test_an_operator_inside_a_customers_account_cannot_write_in_their_name(client, ana, agent):
    ticket = _ticket(ana)
    client.force_login(agent)
    client.post(reverse("accounts:start_impersonation", kwargs={"user_id": ana.pk}))

    client.post(reverse("support:ticket_reply", args=[ticket.pk]), {"body": "em nome dela"})
    client.post(reverse("support:ticket_new"), {"kind": "bug", "subject": "x", "body": "y"})
    client.post(reverse("support:chat_send"), {"body": "oi"})

    assert not TicketMessage.objects.filter(body="em nome dela").exists()
    assert Ticket.objects.count() == 1
    assert not ana.support_chats.exists()


@pytest.mark.django_db
def test_the_team_answers_from_its_screen(client, ana, agent):
    ticket = _ticket(ana)
    client.force_login(agent)

    assert client.get(reverse("support_staff:tickets")).status_code == 200
    assert client.get(reverse("support_staff:ticket", args=[ticket.pk])).status_code == 200
    client.post(reverse("support_staff:ticket_reply", args=[ticket.pk]), {"body": "Resolvido!", "status": "resolved"})

    ticket.refresh_from_db()
    assert ticket.status == "resolved"
    assert ticket.messages.filter(body="Resolvido!", from_team=True, is_internal=False).exists()


@pytest.mark.django_db
def test_a_customer_cannot_open_the_teams_screens(client, ana):
    ticket = _ticket(ana)
    client.force_login(ana)

    assert client.get(reverse("support_staff:ticket", args=[ticket.pk])).status_code == 403
