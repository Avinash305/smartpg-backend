from django.db import migrations


def dedup_invoicesettings(apps, schema_editor):
    InvoiceSettings = apps.get_model('payment', 'InvoiceSettings')
    db_alias = schema_editor.connection.alias

    # Find duplicate (owner, building) groups and keep the newest id
    from django.db.models import Count, Max
    dups = (
        InvoiceSettings.objects.using(db_alias)
        .values('owner_id', 'building_id')
        .annotate(c=Count('id'), keep_id=Max('id'))
        .filter(c__gt=1)
    )
    total_deleted = 0
    for row in dups:
        owner_id = row['owner_id']
        building_id = row['building_id']
        keep_id = row['keep_id']
        qs = InvoiceSettings.objects.using(db_alias).filter(owner_id=owner_id, building_id=building_id).exclude(id=keep_id)
        deleted, _ = qs.delete()
        total_deleted += deleted
    # Optional: print/log info (no-op in migrations on some backends)
    if total_deleted:
        print(f"Deduplicated InvoiceSettings: removed {total_deleted} older duplicates")


def noop_reverse(apps, schema_editor):
    # No reverse action needed
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0011_remove_expensecategory_unique_expensecategory_sequence_per_owner_and_more'),
    ]

    operations = [
        migrations.RunPython(dedup_invoicesettings, noop_reverse),
    ]
