from django.db import migrations

# More lines of business on the shelf a brand chooses its segments from, each
# with its special dates. As in 0008_seed_date_segments, the operator edits
# them from the administrative area and a segment that already exists (by
# name) is left exactly as it is.
SEGMENTS = (
    (
        "Beleza e estética",
        "💇",
        (
            (1, 19, "Dia do Cabeleireiro", "💇"),
            (9, 9, "Dia Internacional da Beleza", "💄"),
        ),
    ),
    (
        "Moda e vestuário",
        "👗",
        (
            (5, 20, "Dia do Jeans", "👖"),
            (5, 25, "Dia da Costureira", "🧵"),
            (9, 6, "Dia do Alfaiate", "🪡"),
        ),
    ),
    (
        "Varejo e e-commerce",
        "🛍️",
        (
            (7, 16, "Dia do Comerciante", "🏪"),
            (10, 1, "Dia do Vendedor", "🤝"),
            (10, 30, "Dia do Comerciário", "🛍️"),
        ),
    ),
    (
        "Imóveis, arquitetura e construção",
        "🏗️",
        (
            (8, 21, "Dia da Habitação", "🏠"),
            (12, 11, "Dia do Engenheiro", "📐"),
            (12, 13, "Dia do Pedreiro", "🧱"),
            (12, 15, "Dia do Arquiteto e Urbanista", "🏛️"),
        ),
    ),
    (
        "Tecnologia",
        "💻",
        (
            (5, 17, "Dia Mundial da Internet", "🌐"),
            (8, 15, "Dia da Informática", "💻"),
            (9, 13, "Dia do Programador", "⌨️"),
            (10, 19, "Dia do Profissional de TI", "🖥️"),
            (11, 30, "Dia Internacional da Segurança da Informação", "🔒"),
        ),
    ),
    (
        "Fitness e esportes",
        "🏋️",
        (
            (2, 19, "Dia do Esportista", "🏅"),
            (4, 6, "Dia Mundial da Atividade Física", "🏃"),
            (6, 23, "Dia Olímpico", "🥇"),
            (7, 19, "Dia Nacional do Futebol", "⚽"),
        ),
    ),
    (
        "Turismo e hotelaria",
        "✈️",
        (
            (3, 2, "Dia Nacional do Turismo", "🗺️"),
            (4, 22, "Dia do Agente de Viagens", "🧳"),
            (9, 27, "Dia Mundial do Turismo", "✈️"),
            (11, 9, "Dia do Hoteleiro", "🏨"),
        ),
    ),
    (
        "Automotivo",
        "🚗",
        (
            (5, 13, "Dia do Automóvel", "🚗"),
            (7, 25, "Dia do Motorista", "🚙"),
            (7, 27, "Dia do Motociclista", "🏍️"),
            (12, 20, "Dia do Mecânico", "🔧"),
        ),
    ),
    (
        "Eventos e festas",
        "🎉",
        (
            (1, 8, "Dia do Fotógrafo", "📷"),
            (4, 30, "Dia do Profissional de Eventos", "🎉"),
            (11, 22, "Dia do Músico", "🎵"),
        ),
    ),
    (
        "Finanças e seguros",
        "💰",
        (
            (8, 13, "Dia do Economista", "📈"),
            (8, 28, "Dia do Bancário", "🏦"),
            (10, 12, "Dia do Corretor de Seguros", "🛡️"),
            (10, 31, "Dia Mundial da Poupança", "💰"),
        ),
    ),
    (
        "Agronegócio",
        "🌾",
        (
            (5, 25, "Dia do Trabalhador Rural", "🚜"),
            (7, 28, "Dia do Agricultor", "🌾"),
            (10, 17, "Dia Nacional da Agricultura", "🌱"),
        ),
    ),
)


def _kind(title):
    if "Mundial" in title or "Internacional" in title or title == "Dia Olímpico":
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
        ("calendar", "0010_fill_special_date_arts"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=migrations.RunPython.noop),
    ]
