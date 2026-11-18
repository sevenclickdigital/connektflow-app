"""Migration 0010 over the starter segments' dates: each gets its production sheet, found
by segment, name and day; one the operator already filled is left alone."""

import importlib
import json

import pytest
from django.apps import apps

from apps.calendar.models import DateSegment, SpecialDate
from apps.tasks.creative_formats import clean_art_items

migration = importlib.import_module("apps.calendar.migrations.0010_fill_special_date_arts")


def test_the_sheets_have_valid_arts():
    sheets = json.loads(migration.SHEETS.read_text(encoding="utf-8"))
    assert len(sheets) == 18
    for sheet in sheets:
        assert sheet["briefing"] and clean_art_items(sheet["art_items"]) == sheet["art_items"]


@pytest.mark.django_db
def test_the_seeded_dates_get_their_sheet_and_the_operators_own_is_kept():
    sheet = json.loads(migration.SHEETS.read_text(encoding="utf-8"))[0]
    segment, _created = DateSegment.objects.get_or_create(name=sheet["segment"])
    SpecialDate.objects.filter(segment=segment).delete()
    seeded = SpecialDate.objects.create(segment=segment, name=sheet["name"], month=sheet["month"], day=sheet["day"])
    edited = SpecialDate.objects.create(
        segment=segment, name=sheet["name"], month=sheet["month"], day=sheet["day"], briefing="Mine"
    )
    elsewhere = SpecialDate.objects.create(name=sheet["name"], month=sheet["month"], day=sheet["day"])

    migration.fill(apps, None)

    seeded.refresh_from_db()
    edited.refresh_from_db()
    elsewhere.refresh_from_db()
    assert (seeded.briefing, seeded.art_items) == (sheet["briefing"], sheet["art_items"])
    assert (edited.briefing, edited.art_items) == ("Mine", [])
    assert elsewhere.art_items == []


def test_every_built_in_date_has_a_sheet():
    from apps.calendar.holidays import BUILT_IN_ARTS, _built_in_arts, _year

    slugs = {slug for _day, slug, *_rest in _year(2027)}
    assert set(BUILT_IN_ARTS) == slugs
    for slug in slugs:
        briefing, items = _built_in_arts(slug)
        assert briefing and clean_art_items(items) == items
