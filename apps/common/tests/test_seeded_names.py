"""A default name stored in the site's language reads in the reader's."""

from django.test import override_settings
from django.utils.translation import gettext_lazy, override

from apps.common.seeded_names import localized_seed_name

DEFAULTS = (gettext_lazy("To do"), gettext_lazy("In progress"))


@override_settings(LANGUAGE_CODE="pt-br")
def test_a_default_name_reads_in_the_active_language():
    with override("pt-br"):
        stored = str(DEFAULTS[0])
    with override("en"):
        assert localized_seed_name(stored, DEFAULTS) == "To do"


@override_settings(LANGUAGE_CODE="pt-br")
def test_a_name_the_brand_chose_stays_as_written():
    with override("en"):
        assert localized_seed_name("Campanha de Natal", DEFAULTS) == "Campanha de Natal"
