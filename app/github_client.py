"""Minimal GitHub REST client: auth (App or PAT), PR files, file contents, reviews."""
from __future__ import annotations

import base64
import logging
import time

import httpx
import jwt

from app.config import Settings

log = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, settings: Settings, installation_id: int | None = None):
        self.settings = settings
        self.base = settings.github_api_url.rstrip("/")
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
            return r.json()["token"]
        if self.settings.github_token:
            return self.settings.github_token
        raise RuntimeError("No GitHub credentials: set GITHUB_APP_ID + private key, or GITHUB_TOKEN")

    # ---------- API ----------
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
