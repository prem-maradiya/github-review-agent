from app import review_service
from app.config import Settings
from app.models import Finding, Severity

PATCH = """@@ -0,0 +1,4 @@
+def run(cmd):
+    result = eval(cmd)
+    return result
+"""
CONTENT = "def run(cmd):\n    result = eval(cmd)\n    return result\n"


class FakeGitHub:
    posted = None

    def __init__(self, settings, installation_id):
        pass

    def list_pr_files(self, repo, number):
        return [
            {"filename": "app/run.py", "status": "added", "patch": PATCH},
            {"filename": "poetry.lock", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ]

    def get_file_content(self, repo, path, ref):
        return CONTENT

    def create_review(self, repo, number, commit_id, body, comments):
        FakeGitHub.posted = {"body": body, "comments": comments, "commit_id": commit_id}
        return {"id": 99}

    def close(self):
        pass


class FakeLLM:
    def __init__(self, settings):
        pass

    def review(self, title, body, files, hints):
        assert [f.path for f in files] == ["app/run.py"]  # lockfile filtered out
        assert any(h.title == "Use of `eval()`" for h in hints)
        return "Looks risky.", [
            # duplicate of AST finding (same line+category) -> dropped
            Finding(path="app/run.py", line=2, severity=Severity.high, category="security",
                    title="eval", body="dup"),
            Finding(path="app/run.py", line=1, severity=Severity.medium, category="bug",
                    title="No input validation", body="Validate `cmd`.", suggestion="def run(cmd: str):"),
            # line not in diff -> dropped
            Finding(path="app/run.py", line=50, severity=Severity.low, category="style", title="x", body="x"),
        ]


def test_full_review_pipeline(monkeypatch):
    monkeypatch.setattr(review_service, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(review_service, "LLMReviewer", FakeLLM)
    settings = Settings(github_webhook_secret="s", github_token="t")

    result = review_service.review_pull_request(settings, "octo/repo", 1, "sha1", "t", "b", None)

    assert result["status"] == "posted"
    comments = FakeGitHub.posted["comments"]
    assert [(c["line"], c["side"]) for c in comments] == [(2, "RIGHT"), (1, "RIGHT")]  # high before medium
    assert "static analysis" in comments[0]["body"]
    assert "```suggestion\ndef run(cmd: str):\n```" in comments[1]["body"]
    assert "Looks risky." in FakeGitHub.posted["body"]
    assert FakeGitHub.posted["commit_id"] == "sha1"


def test_min_severity_filter():
    settings = Settings(github_webhook_secret="s", github_token="t", min_severity="high")
    from app.models import FileDiff
    files = [FileDiff(path="a.py", status="added", patch="", commentable_lines={1, 2})]
    low = Finding(path="a.py", line=1, severity=Severity.low, category="style", title="t", body="b")
    crit = Finding(path="a.py", line=2, severity=Severity.critical, category="security", title="t", body="b")
    assert review_service.merge_findings(files, [low], [crit], settings) == [crit]
