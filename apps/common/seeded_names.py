"""Names the app writes into a brand's data, shown in the reader's language.

Default board columns and the like are stored once, in the site's language
(``settings.LANGUAGE_CODE``), because they are data a brand renames. Someone
using the app in another language would still see them in the site's: this
shows a name that is still the default in the active language instead, and
leaves any name the brand chose as it is.
"""

from collections.abc import Iterable

from django.conf import settings
from django.utils.translation import override


def localized_seed_name(name: str, defaults: Iterable) -> str:
    """``name`` in the active language when it is one of ``defaults``.

    ``defaults`` are the lazy translated strings the name was created from.
    """
    defaults = list(defaults)
    with override(settings.LANGUAGE_CODE):
        stored = [str(default) for default in defaults]
    for default, as_stored in zip(defaults, stored, strict=True):
        if name == as_stored:
            return str(default)
    return name
