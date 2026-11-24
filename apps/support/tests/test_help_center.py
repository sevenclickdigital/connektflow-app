"""The help centre: the pages' tutorials (video and the team's text) and the frequent questions."""

import pytest
from django.urls import reverse

from apps.common.rich_text import render_markdown
from apps.onboarding import help_videos
from apps.onboarding.models import PageHelpVideo
from apps.support.models import FaqEntry

VIDEO = "https://youtu.be/dQw4w9WgXcQ"


@pytest.mark.django_db
def test_a_tutorial_can_be_a_text_without_a_video(client, ana):
    help_videos.save_tutorial("inbox", "", "## Responder\n\nAbra a conversa.")

    assert help_videos.video_for_page("inbox") is None
    [tutorial] = help_videos.tutorials()
    assert tutorial["page"] == "inbox" and tutorial["embed_url"] == ""
    client.force_login(ana)
    page = client.get(reverse("support:tutorial", args=["inbox"]))
    assert b"<h2>Responder</h2>" in page.content and b"<iframe" not in page.content


@pytest.mark.django_db
def test_removing_the_video_keeps_the_text(agent, client):
    help_videos.save_tutorial("calendar", VIDEO, "Passo a passo")
    client.force_login(agent)

    client.post(reverse("onboarding_staff:help_video_save"), {"page": "calendar", "video_url": ""})

    row = PageHelpVideo.objects.get(page="calendar")
    assert (row.video_url, row.tutorial_markdown) == ("", "Passo a passo")
    client.post(
        reverse("onboarding_staff:tutorial_edit", args=["calendar"]), {"video_url": "", "tutorial_markdown": ""}
    )
    assert not PageHelpVideo.objects.exists()


@pytest.mark.django_db
def test_the_operator_writes_a_tutorial(client, agent):
    client.force_login(agent)

    client.post(
        reverse("onboarding_staff:tutorial_edit", args=["dashboard"]),
        {"video_url": VIDEO, "tutorial_markdown": "**Oi**"},
    )

    row = PageHelpVideo.objects.get(page="dashboard")
    assert (row.video_url, row.tutorial_markdown) == (VIDEO, "**Oi**")
    assert client.get(reverse("onboarding_staff:tutorial_edit", args=["nowhere"])).status_code == 404


@pytest.mark.django_db
def test_the_help_centre_lists_tutorials_and_published_questions(client, ana):
    help_videos.save_tutorial("dashboard", VIDEO, "")
    FaqEntry.objects.create(topic="Publicação", question="Como agendar?", answer_markdown="Use o calendário.")
    FaqEntry.objects.create(topic="Publicação", question="Rascunho escondido", answer_markdown="x", is_published=False)
    client.force_login(ana)

    page = client.get(reverse("support:help")).content.decode()

    assert "Como agendar?" in page and "Rascunho escondido" not in page
    # The tutorials have their own pages; the help centre lists them only in a search.
    assert "i.ytimg.com" not in page
    assert "Como agendar?" not in client.get(reverse("support:help"), {"q": "fatura"}).content.decode()
    found = client.get(reverse("support:help"), {"q": "dashboard"}).content.decode()
    assert reverse("support:tutorial", args=["dashboard"]) in found


@pytest.mark.django_db
def test_videos_and_guides_have_their_own_pages(client, ana):
    help_videos.save_tutorial("dashboard", VIDEO, "")
    help_videos.save_tutorial("inbox", "", "Responda pela caixa de entrada.")
    client.force_login(ana)

    videos = client.get(reverse("support:tutorials")).content.decode()
    guides = client.get(reverse("support:guides")).content.decode()

    assert reverse("support:tutorial", args=["dashboard"]) in videos
    assert reverse("support:tutorial", args=["inbox"]) not in videos
    assert reverse("support:tutorial", args=["inbox"]) in guides
    assert reverse("support:tutorial", args=["dashboard"]) not in guides
    assert reverse("support:ticket_new") in videos and reverse("support:ticket_new") in guides


def test_the_teams_markdown_cannot_carry_scripts():
    html = render_markdown(
        "<script>alert(1)</script> [x](javascript:alert(1)) <img src=x onerror=alert(1)> [ok](https://a.b)"
    )

    assert "<script" not in html and "javascript:" not in html and "onerror" not in html and "<img" not in html
    assert 'href="https://a.b"' in html and 'rel="noopener noreferrer"' in html
