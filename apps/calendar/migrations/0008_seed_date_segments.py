from django.db import migrations

# A starter shelf so the "Special dates" tab is not empty on day one. The
# operator edits it from the administrative area; running this again never
# overwrites what they changed (a segment that already exists is left alone).
SEGMENTS = (
    (
        "Alimentação e restaurantes",
        "🍽️",
        (
            (5, 24, "Dia Nacional do Café", "☕"),
            (5, 28, "Dia Mundial do Hambúrguer", "🍔"),
            (7, 7, "Dia Mundial do Chocolate", "🍫"),
            (7, 10, "Dia da Pizza", "🍕"),
            (9, 23, "Dia do Sorvete", "🍦"),
            (10, 16, "Dia Mundial da Alimentação", "🥗"),
        ),
    ),
    (
        "Saúde e bem-estar",
        "🩺",
        (
            (4, 7, "Dia Mundial da Saúde", "🩺"),
            (8, 31, "Dia do Nutricionista", "🥦"),
            (9, 1, "Dia do Profissional de Educação Física", "🏋️"),
            (10, 18, "Dia do Médico", "⚕️"),
            (10, 25, "Dia do Dentista", "🦷"),
        ),
    ),
    (
        "Pet",
        "🐾",
        (
            (9, 9, "Dia do Veterinário", "🐶"),
            (10, 4, "Dia Mundial dos Animais", "🐾"),
        ),
    ),
    (
        "Serviços profissionais",
        "💼",
        (
            (8, 11, "Dia do Advogado", "⚖️"),
            (8, 27, "Dia do Corretor de Imóveis", "🏠"),
            (9, 22, "Dia do Contador", "🧮"),
        ),
    ),
    (
        "Educação",
        "📚",
        (
            (4, 23, "Dia Mundial do Livro", "📖"),
            (8, 11, "Dia do Estudante", "🎓"),
        ),
    ),
)


def _kind(title):
    if "Mundial" in title:
        return "international"
    return "national" if "Nacional" in title else "commercial"


def seed(apps, schema_editor):
    DateSegment = apps.get_model("calendar", "DateSegment")
    SpecialDate = apps.get_model("calendar", "SpecialDate")
    for name, emoji, dates in SEGMENTS:
        segment, created = DateSegment.objects.get_or_create(name=name, defaults={"emoji": emoji})
        if not created:
            continue
        SpecialDate.objects.bulk_create(
            SpecialDate(segment=segment, month=month, day=day, name=title, emoji=icon, kind=_kind(title))
            for month, day, title, icon in dates
        )


class Migration(migrations.Migration):
    dependencies = [
        ("calendar", "0007_special_dates_by_segment"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=migrations.RunPython.noop),
    ]
