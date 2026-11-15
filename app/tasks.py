import logging

import anthropic
import httpx
import redis

from app.celery_app import celery_app
from app.config import get_settings
from app.review_service import review_pull_request

log = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="review_pull_request",
    autoretry_for=(anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError,
                   httpx.TransportError),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=5,
)
def review_pull_request_task(self, repo: str, number: int, head_sha: str, title: str, body: str,
                             installation_id: int | None = None) -> dict:
    settings = get_settings()
    r = redis.Redis.from_url(settings.redis_url)
    # Only one review per PR at a time; a newer push will enqueue its own task.
    lock = r.lock(f"review-lock:{repo}#{number}", timeout=20 * 60, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        log.info("Review already running for %s#%s, retrying shortly", repo, number)
        raise self.retry(countdown=60)
    try:
        # Skip if the PR has moved on to a newer commit while we were queued
        latest = r.get(f"pr-head:{repo}#{number}")
        if latest and latest.decode() != head_sha:
            log.info("Skipping stale review for %s#%s@%s", repo, number, head_sha[:7])
            return {"status": "skipped", "reason": "stale commit"}
        return review_pull_request(settings, repo, number, head_sha, title, body, installation_id)
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass
