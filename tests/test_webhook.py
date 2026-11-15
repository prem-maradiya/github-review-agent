import hashlib
import hmac
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main

SECRET = "test-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    fake = fakeredis.FakeRedis()
    queued = []
    main.app.dependency_overrides[main.get_redis] = lambda: fake
    monkeypatch.setattr(main, "enqueue_review", lambda **kw: queued.append(kw) or "task-123")
    c = TestClient(main.app)
    c.queued, c.redis = queued, fake
    yield c
    main.app.dependency_overrides.clear()


def pr_payload(action="opened", draft=False):
    return {
        "action": action,
        "installation": {"id": 42},
        "repository": {"full_name": "octo/repo"},
        "pull_request": {"number": 7, "draft": draft, "title": "Add feature", "body": "desc",
                         "head": {"sha": "abc1234def"}},
    }


def post(client, payload, event="pull_request", delivery="d-1", signature=None):
    body = json.dumps(payload).encode()
    return client.post("/webhook/github", content=body, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature or sign(body), "Content-Type": "application/json"})


def test_rejects_bad_signature(client):
    assert post(client, pr_payload(), signature="sha256=nope").status_code == 401


def test_ping(client):
    assert post(client, {"zen": "hi"}, event="ping").json() == {"status": "pong"}


def test_queues_review_and_records_head(client):
    resp = post(client, pr_payload())
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued", "task_id": "task-123"}
    assert client.queued[0] == {"repo": "octo/repo", "number": 7, "head_sha": "abc1234def",
                                "title": "Add feature", "body": "desc", "installation_id": 42}
    assert client.redis.get("pr-head:octo/repo#7") == b"abc1234def"


def test_duplicate_delivery_is_ignored(client):
    post(client, pr_payload())
    assert post(client, pr_payload()).json() == {"status": "duplicate"}
    assert len(client.queued) == 1


@pytest.mark.parametrize("payload", [pr_payload(action="closed"), pr_payload(draft=True)])
def test_ignores_irrelevant_prs(client, payload):
    assert post(client, payload).json()["status"] == "ignored"
    assert client.queued == []
