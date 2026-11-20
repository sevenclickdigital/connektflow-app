"""Contacts: tenant isolation, permissions, the inbox link, the form and CSV import."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts import services
from apps.contacts.forms import ContactForm, is_valid_cnpj, is_valid_cpf
from apps.contacts.models import Contact, ContactChannel
from apps.inbox.models import InboxMessage
from apps.radar.tests.test_radar import Role, _client, _user, _workspace
from apps.social_accounts.models import SocialAccount

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english")]


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


def _account(workspace, platform="instagram", ig_id="ig-1"):
    return SocialAccount.objects.create(
        workspace=workspace, platform=platform, account_platform_id=ig_id, account_name="Brand"
    )


def _message(account, handle, *, n="1", kind="comment", days_ago=0, extra=None, name="Carla"):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=f"m-{handle}-{n}",
        message_type=kind,
        sender_name=name,
        sender_handle=handle,
        body="hello",
        extra=extra or {},
        received_at=timezone.now() - timedelta(days=days_ago),
    )


def _list_url(workspace):
    return reverse("contacts:list", kwargs={"workspace_id": workspace.id})


class TestInboxLink:
    def test_a_sender_becomes_one_contact_whatever_the_number_of_messages(self):
        workspace = _workspace("A")
        account = _account(workspace)
        for n in range(3):
            message = _message(account, "1789", n=str(n), extra={"sender_handle": "carla.arq"})
            services.record_inbox_sender(message.pk)

        contact = Contact.objects.get(workspace=workspace)
        channel = contact.channels.get()
        assert (channel.kind, channel.value, channel.external_id) == ("instagram", "@carla.arq", "1789")
        assert contact.source == "instagram"
        assert (contact.interactions_90d, contact.engagement) == (3, "medium")

    def test_the_same_person_by_handle_and_by_id_is_not_duplicated(self):
        workspace = _workspace("A")
        account = _account(workspace)
        services.record_inbox_sender(_message(account, "1789", extra={"sender_handle": "carla.arq"}).pk)
        services.record_inbox_sender(_message(account, "carla.arq", n="2").pk)

        assert Contact.objects.filter(workspace=workspace).count() == 1
        assert Contact.objects.get(workspace=workspace).interactions_90d == 2

    def test_a_contact_typed_by_hand_takes_the_inbox_messages(self):
        workspace = _workspace("A")
        account = _account(workspace)
        typed = Contact.objects.create(workspace=workspace, name="Carla Martins")
        ContactChannel.objects.create(workspace=workspace, contact=typed, kind="instagram", value="@carla.arq")

        services.record_inbox_sender(_message(account, "carla.arq").pk)

        assert list(Contact.objects.filter(workspace=workspace)) == [typed]
        assert typed.channels.get().external_id == "carla.arq"

    def test_the_same_id_in_another_workspace_is_another_contact(self):
        mine, theirs = _workspace("A"), _workspace("B")
        services.record_inbox_sender(_message(_account(mine), "1789").pk)
        services.record_inbox_sender(_message(_account(theirs, ig_id="ig-2"), "1789").pk)

        assert Contact.objects.filter(workspace=mine).count() == 1
        assert Contact.objects.filter(workspace=theirs).count() == 1

    def test_a_facebook_dm_is_a_messenger_channel(self):
        workspace = _workspace("A")
        account = _account(workspace, platform="facebook", ig_id="page-1")
        services.record_inbox_sender(_message(account, "3809276499137463", kind="dm", name="Roberto Silva").pk)

        channel = ContactChannel.objects.get(workspace=workspace)
        assert (channel.kind, channel.value, channel.contact.name) == ("messenger", "Roberto Silva", "Roberto Silva")

    def test_the_daily_pass_imports_old_senders_and_lets_quiet_ones_decay(self):
        workspace = _workspace("A")
        account = _account(workspace)
        _message(account, "old", days_ago=400)
        quiet = Contact.objects.create(
            workspace=workspace,
            name="Quiet",
            interactions_90d=7,
            engagement="high",
            last_contact_at=timezone.now() - timedelta(days=120),
        )

        services.refresh_all()
        services.refresh_all()

        assert ContactChannel.objects.filter(workspace=workspace, external_id="old").count() == 1
        quiet.refresh_from_db()
        assert (quiet.interactions_90d, quiet.engagement) == (0, "low")

    def test_the_manual_level_wins_over_the_computed_one(self):
        contact = Contact(name="X", interactions_90d=9, engagement_manual="low")
        contact.refresh_engagement()
        assert contact.engagement == "low"


class TestListAndPermissions:
    def test_the_list_shows_only_this_workspaces_contacts(self):
        mine, theirs = _workspace("A"), _workspace("B")
        Contact.objects.create(workspace=mine, name="Carla Martins")
        Contact.objects.create(workspace=theirs, name="Foreign Person")
        client = _client(_user("a@x.com", mine, Role.EDITOR))

        response = client.get(_list_url(mine))

        page = response.content.decode()
        assert response.status_code == 200
        assert "Carla Martins" in page
        assert "Foreign Person" not in page

    def test_filters_narrow_the_list(self):
        workspace = _workspace("A")
        lead = Contact.objects.create(workspace=workspace, name="Lead One", status="lead", tags=["Client"])
        Contact.objects.create(workspace=workspace, name="Active One", status="active")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.get(_list_url(workspace), {"status": "lead", "tag": "Client"})

        assert list(response.context["page"].object_list) == [lead]

    def test_the_facebook_pill_covers_messenger_contacts(self):
        workspace = _workspace("A")
        on_messenger = Contact.objects.create(workspace=workspace, name="Roberto")
        ContactChannel.objects.create(workspace=workspace, contact=on_messenger, kind="messenger", value="Roberto")
        Contact.objects.create(workspace=workspace, name="No channel")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.get(_list_url(workspace), {"channel": "facebook"})

        assert list(response.context["page"].object_list) == [on_messenger]

    def test_a_viewer_cannot_see_contacts(self):
        workspace = _workspace("A")
        client = _client(_user("v@x.com", workspace, Role.VIEWER))

        assert client.get(_list_url(workspace)).status_code == 403

    def test_another_workspace_cannot_edit_or_delete_a_contact(self):
        owner, other = _workspace("A"), _workspace("B")
        contact = Contact.objects.create(workspace=owner, name="Carla")
        client = _client(_user("b@x.com", other, Role.EDITOR))
        kwargs = {"workspace_id": other.id, "contact_id": contact.id}

        assert client.get(reverse("contacts:edit", kwargs=kwargs)).status_code == 404
        assert client.post(reverse("contacts:delete", kwargs=kwargs)).status_code == 404
        client.post(reverse("contacts:bulk_delete", kwargs={"workspace_id": other.id}), {"ids": [str(contact.id)]})
        assert Contact.objects.filter(pk=contact.pk).exists()

    def test_bulk_delete_answers_with_a_toast_and_a_refresh(self):
        workspace = _workspace("A")
        contacts = [Contact.objects.create(workspace=workspace, name=f"C{i}") for i in range(2)]
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.post(
            reverse("contacts:bulk_delete", kwargs={"workspace_id": workspace.id}),
            {"ids": [str(c.id) for c in contacts]},
        )

        assert response.status_code == 204
        assert "contactsChanged" in response["HX-Trigger"]
        assert not Contact.objects.filter(workspace=workspace).exists()


class TestForm:
    def _post(self, client, workspace, **extra):
        data = {
            "name": "Carla Martins",
            "kind": "person",
            "status": "active",
            "source": "instagram",
            "tags": "Client, Interest, client",
            "channel_kind": ["whatsapp", "instagram", "email"],
            "channel_value": ["+55 48 99123-4567", "carlamartins.arq", "Carla@MartinsArq.com.br"],
            "channel_primary": "0",
            "postal_code": "88000000",
            "state": "SC",
            **extra,
        }
        return client.post(reverse("contacts:create", kwargs={"workspace_id": workspace.id}), data)

    def test_a_contact_is_saved_with_its_channels_and_tags(self):
        workspace = _workspace("A")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = self._post(client, workspace)

        assert response.status_code == 302
        contact = Contact.objects.get(workspace=workspace)
        assert contact.tags == ["Client", "Interest"]
        assert contact.postal_code == "88000-000"
        assert list(contact.channels.values_list("kind", "value", "is_primary")) == [
            ("whatsapp", "+5548991234567", True),
            ("instagram", "@carlamartins.arq", False),
            ("email", "carla@martinsarq.com.br", False),
        ]

    def test_an_invalid_cpf_is_refused(self):
        workspace = _workspace("A")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = self._post(client, workspace, tax_id="111.222.333-44")

        assert response.status_code == 200
        assert "Enter a valid CPF or CNPJ." in response.content.decode()
        assert not Contact.objects.exists()

    def test_editing_keeps_the_inbox_link_of_an_unchanged_channel(self):
        workspace = _workspace("A")
        contact = Contact.objects.create(workspace=workspace, name="Carla")
        ContactChannel.objects.create(
            workspace=workspace, contact=contact, kind="instagram", value="@carla", external_id="1789"
        )
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.post(
            reverse("contacts:edit", kwargs={"workspace_id": workspace.id, "contact_id": contact.id}),
            {
                "name": "Carla M.",
                "kind": "person",
                "status": "lead",
                "source": "manual",
                "channel_kind": ["instagram"],
                "channel_value": ["@carla"],
            },
        )

        assert response.status_code == 302
        assert contact.channels.get().external_id == "1789"

    def test_the_form_pages_render(self):
        workspace = _workspace("A")
        contact = Contact.objects.create(workspace=workspace, name="Carla")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        assert client.get(reverse("contacts:create", kwargs={"workspace_id": workspace.id})).status_code == 200
        edit = client.get(reverse("contacts:edit", kwargs={"workspace_id": workspace.id, "contact_id": contact.id}))
        assert edit.status_code == 200
        assert "Interactions (0)" in edit.content.decode()


class TestCsvAndDocuments:
    def test_import_creates_contacts_and_skips_known_channels(self):
        workspace = _workspace("A")
        known = Contact.objects.create(workspace=workspace, name="Known")
        ContactChannel.objects.create(workspace=workspace, contact=known, kind="email", value="known@x.com")
        raw = (
            b"name,whatsapp,email,tags,status\n"
            b'Ana Clara,+55 48 99777-8899,,"Client, VIP",active\n'
            b"Known Again,,known@x.com,,\n"
            b",,,,\n"
        )

        result = services.import_csv(workspace.id, raw)

        assert (result.created, result.skipped) == (1, 2)
        ana = Contact.objects.get(workspace=workspace, name="Ana Clara")
        assert (ana.status, ana.tags, ana.source) == ("active", ["Client", "VIP"], "import")
        assert ana.channels.get().value == "+5548997778899"

    def test_export_neutralises_spreadsheet_formulas(self):
        workspace = _workspace("A")
        Contact.objects.create(workspace=workspace, name="=HYPERLINK(1)")

        out = services.export_csv(Contact.objects.for_workspace(workspace.id))

        assert "'=HYPERLINK(1)" in out

    def test_cpf_and_cnpj_check_digits(self):
        assert is_valid_cpf("52998224725")
        assert not is_valid_cpf("52998224724")
        assert not is_valid_cpf("11111111111")
        assert is_valid_cnpj("11222333000181")
        assert not is_valid_cnpj("11222333000182")


class TestMerge:
    def _url(self, workspace):
        return reverse("contacts:merge", kwargs={"workspace_id": workspace.id})

    def test_two_networks_become_one_contact(self):
        workspace = _workspace("A")
        ig = _account(workspace)
        fb = _account(workspace, platform="facebook", ig_id="page-1")
        services.record_inbox_sender(_message(ig, "1789", extra={"sender_handle": "carla.arq"}).pk)
        services.record_inbox_sender(_message(fb, "3809276499137463", kind="dm", name="Carla Martins").pk)
        keep = ContactChannel.objects.get(kind="instagram").contact
        other = ContactChannel.objects.get(kind="messenger").contact
        Contact.objects.filter(pk=keep.pk).update(tags=["Client"], notes="First")
        Contact.objects.filter(pk=other.pk).update(tags=["VIP", "client"], company="Martins Arq", notes="Second")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.post(self._url(workspace), {"ids": [str(keep.id), str(other.id)], "keep": str(keep.id)})

        assert response.status_code == 204
        assert "contactsChanged" in response["HX-Trigger"]
        keep.refresh_from_db()
        assert not Contact.objects.filter(pk=other.pk).exists()
        assert sorted(keep.channels.values_list("kind", flat=True)) == ["instagram", "messenger"]
        assert keep.channels.filter(is_primary=True).count() == 1
        assert (keep.tags, keep.company) == (["Client", "VIP"], "Martins Arq")
        assert keep.notes == "First\n\nMerged from Carla Martins\nContact source: Facebook\n\nSecond"
        assert keep.interactions_90d == 2

    def test_a_shared_channel_is_not_duplicated_and_keeps_the_inbox_link(self):
        workspace = _workspace("A")
        keep = Contact.objects.create(workspace=workspace, name="Typed")
        ContactChannel.objects.create(workspace=workspace, contact=keep, kind="instagram", value="@carla")
        other = Contact.objects.create(workspace=workspace, name="From inbox")
        ContactChannel.objects.create(
            workspace=workspace, contact=other, kind="instagram", value="@carla", external_id="1789"
        )

        services.merge_contacts(workspace.id, keep.id, [keep.id, other.id])

        channel = keep.channels.get()
        assert channel.external_id == "1789"

    def test_another_workspaces_contact_cannot_be_merged(self):
        mine, theirs = _workspace("A"), _workspace("B")
        keep = Contact.objects.create(workspace=mine, name="Mine")
        foreign = Contact.objects.create(workspace=theirs, name="Foreign")
        client = _client(_user("a@x.com", mine, Role.EDITOR))

        response = client.post(self._url(mine), {"ids": [str(keep.id), str(foreign.id)], "keep": str(keep.id)})

        assert "could not be found" in response["HX-Trigger"]
        assert Contact.objects.filter(pk=foreign.pk).exists()

    def test_conflicting_values_are_written_into_the_notes_and_the_kept_ones_stay(self):
        workspace = _workspace("A")
        keep = Contact.objects.create(workspace=workspace, name="Carla", company="Studio A", status="lead")
        other = Contact.objects.create(
            workspace=workspace,
            name="Carla Martins",
            company="Martins Arq",
            status="active",
            birth_date=date(1990, 3, 7),
            avatar_url="https://cdn.example.com/a.jpg",
            notes="Prefers WhatsApp",
        )
        Contact.objects.filter(pk=keep.pk).update(
            birth_date=date(1991, 1, 1), avatar_url="https://cdn.example.com/b.jpg"
        )

        services.merge_contacts(workspace.id, keep.id, [other.id])

        keep.refresh_from_db()
        assert (keep.name, keep.company, keep.status) == ("Carla", "Studio A", "lead")
        assert keep.birth_date == date(1991, 1, 1)
        lines = keep.notes.split("\n")
        assert lines[0] == "Merged from Carla Martins"
        assert "Company: Martins Arq" in lines
        assert "Status: Active" in lines
        assert any(line.startswith("Date of birth: ") and "1990" in line for line in lines)
        assert not any("cdn.example.com" in line for line in lines)
        assert keep.notes.endswith("\n\nPrefers WhatsApp")

    def test_identical_contacts_only_append_the_notes(self):
        workspace = _workspace("A")
        keep = Contact.objects.create(workspace=workspace, name="Carla", company="Studio A", notes="First")
        other = Contact.objects.create(workspace=workspace, name="Carla", company="", notes="Second")

        services.merge_contacts(workspace.id, keep.id, [other.id])

        keep.refresh_from_db()
        assert keep.notes == "First\n\nSecond"

    def test_long_notes_are_kept_whole_and_the_contact_stays_editable(self):
        workspace = _workspace("A")
        first, second = "a" * services.MAX_NOTES_LENGTH, "b" * services.MAX_NOTES_LENGTH
        keep = Contact.objects.create(workspace=workspace, name="Carla", notes=first)
        other = Contact.objects.create(workspace=workspace, name="Carla", notes=second)

        services.merge_contacts(workspace.id, keep.id, [other.id])

        keep.refresh_from_db()
        assert keep.notes == f"{first}\n\n{second}"
        form = ContactForm(
            instance=keep,
            data={"name": "Carla", "kind": "person", "status": "lead", "source": "manual", "notes": keep.notes},
        )
        form.is_valid()
        assert "notes" not in form.errors
        too_long = ContactForm(
            instance=keep,
            data={"name": "Carla", "kind": "person", "status": "lead", "source": "manual", "notes": keep.notes + "c"},
        )
        too_long.is_valid()
        assert "notes" in too_long.errors

    def test_a_single_contact_is_not_a_merge(self):
        workspace = _workspace("A")
        keep = Contact.objects.create(workspace=workspace, name="Alone")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.post(self._url(workspace), {"ids": [str(keep.id)], "keep": str(keep.id)})

        assert "at least two" in response["HX-Trigger"]


class TestInboxPanel:
    def test_the_entry_shows_its_contact_and_a_link_to_the_profile(self):
        workspace = _workspace("A")
        account = _account(workspace)
        message = _message(account, "1789", extra={"sender_handle": "carla.arq"})
        contact = services.record_inbox_sender(message.pk)
        Contact.objects.filter(pk=contact.pk).update(name="Carla Martins", tags=["VIP"], funnel="Projeto 3D")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        response = client.get(
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": message.id}),
            HTTP_HX_REQUEST="true",
        )

        page = response.content.decode()
        assert response.context["contact"] == contact
        assert reverse("contacts:edit", kwargs={"workspace_id": workspace.id, "contact_id": contact.id}) in page
        assert "Projeto 3D" in page

    def test_a_contact_of_another_workspace_is_never_shown(self):
        mine, theirs = _workspace("A"), _workspace("B")
        message = _message(_account(mine), "1789")
        foreign = Contact.objects.create(workspace=theirs, name="Foreign")
        ContactChannel.objects.create(
            workspace=theirs, contact=foreign, kind="instagram", value="x", external_id="1789"
        )

        assert services.contact_for_message(message) is None

    def _carla_on_two_networks(self, workspace):
        ig = _account(workspace)
        fb = _account(workspace, platform="facebook", ig_id="page-1")
        ig_dm = _message(ig, "1789", kind="dm", extra={"sender_handle": "carla.arq"}, name="carla.arq", days_ago=1)
        fb_dm = _message(fb, "3809", kind="dm", name="Carla Martins")
        keep = services.record_inbox_sender(ig_dm.pk)
        other = services.record_inbox_sender(fb_dm.pk)
        return ig_dm, fb_dm, keep, other

    def test_after_a_merge_the_conversation_links_the_persons_other_channel(self):
        workspace = _workspace("A")
        ig_dm, fb_dm, keep, other = self._carla_on_two_networks(workspace)
        client = _client(_user("a@x.com", workspace, Role.EDITOR))
        detail = reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": ig_dm.id})

        assert client.get(detail, HTTP_HX_REQUEST="true").context["person_channels"] == []

        client.post(
            reverse("contacts:merge", kwargs={"workspace_id": workspace.id}),
            {"ids": [str(keep.id), str(other.id)], "keep": str(keep.id)},
        )
        response = client.get(detail, HTTP_HX_REQUEST="true")

        chips = response.context["person_channels"]
        assert [(chip["message"], chip["kind"], chip["active"]) for chip in chips] == [
            (fb_dm, "messenger", False),
            (ig_dm, "instagram", True),
        ]
        page = response.content.decode()
        assert reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": fb_dm.id}) in page

    def test_the_main_contact_can_be_the_other_one(self):
        workspace = _workspace("A")
        ig_dm, fb_dm, on_screen, main = self._carla_on_two_networks(workspace)
        client = _client(_user("a@x.com", workspace, Role.EDITOR))

        client.post(
            reverse("contacts:merge", kwargs={"workspace_id": workspace.id}),
            {"ids": [str(on_screen.id), str(main.id)], "keep": str(main.id)},
        )
        response = client.get(
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": ig_dm.id}),
            HTTP_HX_REQUEST="true",
        )

        assert not Contact.objects.filter(pk=on_screen.pk).exists()
        assert response.context["contact"] == main
        assert [chip["message"] for chip in response.context["person_channels"]] == [fb_dm, ig_dm]

    def test_the_list_shows_the_other_channels_of_a_merged_sender(self):
        workspace = _workspace("A")
        ig_dm, fb_dm, keep, other = self._carla_on_two_networks(workspace)
        services.merge_contacts(workspace.id, keep.id, [keep.id, other.id])

        result = services.other_channels_by_message(workspace.id, [ig_dm, fb_dm])

        assert result == {ig_dm.id: ["messenger"], fb_dm.id: ["instagram"]}

    def test_the_merge_dialog_suggests_a_contact_with_the_same_first_name(self):
        workspace = _workspace("A")
        _ig_dm, _fb_dm, keep, other = self._carla_on_two_networks(workspace)
        Contact.objects.create(workspace=workspace, name="Bruno")
        foreign = Contact.objects.create(workspace=_workspace("B"), name="Carla Foreign")
        client = _client(_user("a@x.com", workspace, Role.EDITOR))
        url = reverse("contacts:merge_candidates", kwargs={"workspace_id": workspace.id, "contact_id": keep.id})

        suggested = client.get(url).context["candidates"]
        searched = client.get(url, {"q": "bru"}).context["candidates"]

        assert suggested == [other]
        assert [c.name for c in searched] == ["Bruno"]
        assert foreign not in suggested

    def test_the_merge_dialog_refuses_another_workspaces_contact(self):
        mine, theirs = _workspace("A"), _workspace("B")
        foreign = Contact.objects.create(workspace=theirs, name="Foreign")
        client = _client(_user("a@x.com", mine, Role.EDITOR))

        response = client.get(
            reverse("contacts:merge_candidates", kwargs={"workspace_id": mine.id, "contact_id": foreign.id})
        )

        assert response.status_code == 404

    def test_the_merge_summary_counts_channels_interactions_and_filled_fields(self):
        workspace = _workspace("A")
        now = timezone.now()
        contact = Contact.objects.create(
            workspace=workspace,
            name="Carla",
            company="Studio",
            tags=["VIP"],
            city="Recife",
            avatar_url="https://example.com/a.jpg",
            interactions_90d=12,
            last_contact_at=now - timedelta(days=2),
        )
        for value in ("carla.arq", "carla@example.com"):
            ContactChannel.objects.create(
                workspace=workspace, contact=contact, kind="instagram" if "@" not in value else "email", value=value
            )
        empty = Contact.objects.create(workspace=workspace, name="Carla M", tags=[])

        summary = services.merge_summary(contact, now=now)

        assert (summary["channels"], summary["interactions"], summary["fields"]) == (2, 12, 4)
        assert summary["text"] == "2 channels · 12 interactions · 4 fields filled · last contact 2\xa0days ago"
        assert services.merge_summary(empty, now=now)["text"] == "0 channels · 0 interactions · 0 fields filled"

    def test_the_merge_dialog_marks_the_most_complete_contact(self):
        workspace = _workspace("A")
        _ig_dm, _fb_dm, on_screen, other = self._carla_on_two_networks(workspace)
        client = _client(_user("a@x.com", workspace, Role.EDITOR))
        url = reverse("contacts:merge_candidates", kwargs={"workspace_id": workspace.id, "contact_id": on_screen.id})
        Contact.objects.filter(pk__in=[on_screen.pk, other.pk]).update(interactions_90d=0)

        tied = client.get(url)
        Contact.objects.filter(pk=other.pk).update(company="Studio", city="Recife")
        marked = client.get(url)

        assert tied.context["most_complete_id"] is None
        assert "Most complete" not in tied.content.decode()
        assert marked.context["most_complete_id"] == other.pk
        page = marked.content.decode()
        assert page.count("Most complete") == 1
        assert f'value="{other.id}"' in page
        assert "2 fields filled" in page


class TestPhoto:
    def _png(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def test_a_photo_is_renamed_to_the_extension_of_its_bytes(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.contacts.forms import clean_photo

        upload = SimpleUploadedFile("carla.html", self._png(), content_type="image/png")

        assert clean_photo(upload) == ""
        assert upload.name == "carla.png"

    def test_a_file_that_is_not_an_image_is_refused_whatever_it_declares(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.contacts.forms import clean_photo

        upload = SimpleUploadedFile("carla.png", b"<!doctype html><p>x</p>", content_type="image/png")

        assert clean_photo(upload) != ""
