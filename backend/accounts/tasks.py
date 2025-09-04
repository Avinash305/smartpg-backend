from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging

from .models import ActivityLog


@shared_task(name="accounts.tasks.purge_old_activities")
def purge_old_activities(days: int = 30, chunk_size: int = 500) -> int:
    """Delete ActivityLog rows older than `days` days.

    Uses chunked deletes to avoid large single queries. Returns number of rows deleted.
    """
    try:
        days = int(days)
        chunk_size = max(100, int(chunk_size))
    except Exception:
        days = 30
        chunk_size = 500

    cutoff = timezone.now() - timedelta(days=days)
    logger = logging.getLogger(__name__)

    # Collect pks in ascending order and delete in batches
    qs = ActivityLog.objects.filter(timestamp__lt=cutoff).order_by("timestamp").values_list("pk", flat=True)
    total_deleted = 0
    batch: list[int] = []

    for pk in qs.iterator(chunk_size=1000):
        batch.append(pk)
        if len(batch) >= chunk_size:
            ActivityLog.objects.filter(pk__in=batch).delete()
            total_deleted += len(batch)
            batch.clear()

    if batch:
        ActivityLog.objects.filter(pk__in=batch).delete()
        total_deleted += len(batch)

    logger.info(
        "Purged %s ActivityLog entries older than %s days (cutoff %s)",
        total_deleted,
        days,
        cutoff.isoformat(),
    )
    return total_deleted
