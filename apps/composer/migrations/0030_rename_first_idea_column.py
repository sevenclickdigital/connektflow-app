from django.db import migrations

#: Position on the board → (the names the column was created with, its new
#: name). The board stopped being a to-do list and became where a team sorts
#: its ideas, so the columns say what an idea is worth, not who is doing it.
RENAMES = {
    0: (("Sem responsável", "Unassigned"), "Brainstorming"),
    1: (("A fazer", "To Do"), "Em avaliação"),
    2: (("Em andamento", "In Progress"), "Aprovadas"),
    3: (("Pronto", "Done"), "Descartadas"),
}

#: The last column changes meaning, not only name ("done" becomes
#: "discarded"), so it is renamed only where it holds no idea.
ONLY_IF_EMPTY = {3}


def rename(apps, schema_editor):
    """Give the columns of each idea board their new names.

    Only a column still carrying the name it was created with: one a brand
    renamed is its own and is left alone. A few short UPDATEs over a small
    table (a handful of columns per brand).
    """
    IdeaGroup = apps.get_model("composer", "IdeaGroup")
    for position, (old_names, new_name) in RENAMES.items():
        columns = IdeaGroup.objects.filter(position=position, name__in=old_names)
        if position in ONLY_IF_EMPTY:
            columns = columns.filter(ideas__isnull=True)
        columns.update(name=new_name)


def restore(apps, schema_editor):
    IdeaGroup = apps.get_model("composer", "IdeaGroup")
    for position, (old_names, new_name) in RENAMES.items():
        IdeaGroup.objects.filter(position=position, name=new_name).update(name=old_names[0])


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0029_seed_idea_templates"),
    ]

    operations = [
        migrations.RunPython(rename, reverse_code=restore),
    ]
