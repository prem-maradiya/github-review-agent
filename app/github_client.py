"""Minimal GitHub REST client: auth (App or PAT), PR files, file contents, reviews."""
from __future__ import annotations

import base64
import logging
import time

import httpx
import jwt

from app.config import Settings

log = logging.getLogger(__name__)

CHECK_NAME = "AI Code Review"
# Check-run conclusion -> commit-status state (PAT fallback; the Checks API needs a GitHub App)
STATUS_STATE = {"success": "success", "failure": "failure", "neutral": "error"}


class GitHubClient:
    def __init__(self, settings: Settings, installation_id: int | None = None):
        self.settings = settings
        self.base = settings.github_api_url.rstrip("/")
        self.is_app = False
        token = self._resolve_token(installation_id)
        self.http = httpx.Client(
            base_url=self.base,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-code-review-agent",
            },
        )

    # ---------- auth ----------
    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.settings.github_app_id}
        return jwt.encode(payload, self.settings.private_key(), algorithm="RS256")

    def _resolve_token(self, installation_id: int | None) -> str:
        if self.settings.github_app_id and self.settings.private_key() and installation_id:
            r = httpx.post(
                f"{self.base}/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._app_jwt()}", "Accept": "application/vnd.github+json"},
                timeout=30.0,
            )
            r.raise_for_status()
            self.is_app = True
            return r.json()["token"]
        if self.settings.github_token:
            return self.settings.github_token
        raise RuntimeError("No GitHub credentials: set GITHUB_APP_ID + private key, or GITHUB_TOKEN")

    # ---------- API ----------
    def get_pull(self, repo: str, number: int) -> dict:
        r = self.http.get(f"/repos/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    def add_reaction(self, repo: str, comment_id: int, content: str = "eyes") -> None:
        r = self.http.post(f"/repos/{repo}/issues/comments/{comment_id}/reactions", json={"content": content})
        if r.status_code >= 400:
            log.warning("Could not react to comment %s: %s", comment_id, r.status_code)

    # ---------- PR status: check run (GitHub App) or commit status (PAT) ----------
    def start_check(self, repo: str, sha: str) -> int | None:
        """Mark the review as running. Returns a check-run id, or None when using commit statuses."""
        try:
            if self.is_app:
                r = self.http.post(f"/repos/{repo}/check-runs",
                                   json={"name": CHECK_NAME, "head_sha": sha, "status": "in_progress"})
                r.raise_for_status()
                return r.json()["id"]
            self._set_status(repo, sha, "pending", "Review in progress…")
        except httpx.HTTPError as e:
            log.warning("Could not start PR check: %s", e)
        return None

    def finish_check(self, repo: str, sha: str, check_id: int | None, conclusion: str, title: str, summary: str):
        """conclusion: success | failure | neutral"""
        try:
            if check_id is not None:
                r = self.http.patch(f"/repos/{repo}/check-runs/{check_id}", json={
                    "status": "completed", "conclusion": conclusion,
                    "output": {"title": title, "summary": summary[:65000]},
                })
                r.raise_for_status()
            elif not self.is_app:
                self._set_status(repo, sha, STATUS_STATE[conclusion], title)
        except httpx.HTTPError as e:
            log.warning("Could not finish PR check: %s", e)

    def _set_status(self, repo: str, sha: str, state: str, description: str):
        r = self.http.post(f"/repos/{repo}/statuses/{sha}",
                           json={"state": state, "context": CHECK_NAME, "description": description[:140]})
        r.raise_for_status()

    def list_pr_files(self, repo: str, number: int) -> list[dict]:
        files: list[dict] = []
        page = 1
        while True:
            r = self.http.get(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            files.extend(batch)
            if len(batch) < 100 or page >= 30:  # GitHub caps at 3000 files
                return files
            page += 1

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        r = self.http.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") != "base64" or data.get("size", 0) > 1_000_000:
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def create_review(self, repo: str, number: int, commit_id: str, body: str, comments: list[dict]) -> dict:
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments}
        r = self.http.post(f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        if r.status_code == 422 and comments:
            # A comment pointed at a line GitHub rejects; fall back to a summary-only review
            log.warning("Review with inline comments rejected (%s); retrying as summary only", r.text[:300])
            fallback = body + "\n\n<details><summary>Inline comments (could not be anchored)</summary>\n\n"
            fallback += "\n\n".join(f"**{c['path']}:{c['line']}**\n{c['body']}" for c in comments)
            fallback += "\n</details>"
            r = self.http.post(f"/repos/{repo}/pulls/{number}/reviews",
                               json={"commit_id": commit_id, "body": fallback, "event": "COMMENT"})
        r.raise_for_status()
        return r.json()

    def close(self):
        self.http.close()
