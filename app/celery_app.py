from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery("review_agent", broker=settings.redis_url, backend=settings.redis_url, include=["app.tasks"])
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,            # re-deliver if a worker dies mid-review
    worker_prefetch_multiplier=1,   # reviews are long; don't hoard tasks
    task_time_limit=15 * 60,
    task_soft_time_limit=14 * 60,
    result_expires=24 * 3600,
)
