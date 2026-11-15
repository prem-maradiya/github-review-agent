"""FastAPI app: receives GitHub webhooks and enqueues reviews on Celery."""
import json
import logging

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.security import verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="AI Code Review Agent", version="1.0.0")

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def get_redis(settings: Settings = Depends(get_settings)) -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def enqueue_review(**kwargs) -> str:
    from app.tasks import review_pull_request_task  # lazy import keeps the API importable without a broker
    return review_pull_request_task.delay(**kwargs).id


@app.get("/health")
def health(r: redis.Redis = Depends(get_redis)):
    try:
        r.ping()
        return {"status": "ok", "redis": "ok"}
    except redis.RedisError:
        raise HTTPException(503, detail={"status": "degraded", "redis": "unreachable"})


@app.post("/webhook/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
    settings: Settings = Depends(get_settings),
    r: redis.Redis = Depends(get_redis),
):
    raw = await request.body()
    if not verify_signature(raw, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(401, "Invalid signature")

    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event {x_github_event}"}

    payload = json.loads(raw)
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    if action not in REVIEW_ACTIONS:
        return {"status": "ignored", "reason": f"action {action}"}
    if pr.get("draft"):
        return {"status": "ignored", "reason": "draft PR"}

    # GitHub may redeliver; process each delivery once.
    if not r.set(f"delivery:{x_github_delivery}", 1, nx=True, ex=24 * 3600):
        return {"status": "duplicate"}

    repo = payload["repository"]["full_name"]
    number = pr["number"]
    head_sha = pr["head"]["sha"]
    r.set(f"pr-head:{repo}#{number}", head_sha, ex=7 * 24 * 3600)  # lets workers drop stale jobs

    task_id = enqueue_review(
        repo=repo,
        number=number,
        head_sha=head_sha,
        title=pr.get("title", ""),
        body=pr.get("body") or "",
        installation_id=(payload.get("installation") or {}).get("id"),
    )
    log.info("Queued review %s for %s#%s@%s", task_id, repo, number, head_sha[:7])
    return {"status": "queued", "task_id": task_id}


@app.get("/tasks/{task_id}")
def task_status(task_id: str):
    from app.celery_app import celery_app
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "state": res.state, "result": res.result if res.successful() else None}
