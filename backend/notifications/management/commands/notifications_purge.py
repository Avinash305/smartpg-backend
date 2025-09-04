from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from notifications.models import Notification

class Command(BaseCommand):
    help = "Purge notifications by age. Defaults to deleting read notifications older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than",
            type=int,
            default=90,
            help="Age threshold in days. Deletes notifications created before today-N days.",
        )
        parser.add_argument(
            "--include-unread",
            action="store_true",
            help="Also delete unread notifications (by default only read notifications are purged).",
        )
        parser.add_argument(
            "--pg-admin-id",
            type=int,
            default=None,
            help="Restrict purge to a specific PG Admin (by id).",
        )
        parser.add_argument(
            "--building-id",
            type=int,
            default=None,
            help="Restrict purge to a specific Building (by id).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not delete; only print how many would be deleted.",
        )

    def handle(self, *args, **options):
        days = options["older_than"]
        include_unread = options["include_unread"]
        pg_admin_id = options["pg_admin_id"]
        building_id = options["building_id"]
        dry_run = options["dry_run"]

        if days < 0:
            raise CommandError("--older-than must be >= 0")

        cutoff = timezone.now() - timezone.timedelta(days=days)
        qs = Notification.objects.filter(created_at__lt=cutoff)
        if not include_unread:
            qs = qs.filter(unread=False)
        if pg_admin_id:
            qs = qs.filter(pg_admin_id=pg_admin_id)
        if building_id:
            qs = qs.filter(building_id=building_id)

        count = qs.count()
        if dry_run:
            self.stdout.write(self.style.WARNING(f"[DRY-RUN] Would delete {count} notification(s) older than {days} day(s)."))
            return

        deleted, _map = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} notification object(s) older than {days} day(s)."))
