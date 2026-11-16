import pytest

from app import review_service
from app.config import Settings
from app.models import FileDiff, Finding, Severity

PATCH = """@@ -0,0 +1,4 @@
+def run(cmd):
+    result = eval(cmd)
+    return result
+"""
CONTENT = "def run(cmd):\n    result = eval(cmd)\n    return result\n"


class FakeGitHub:
    """Records what the pipeline sends to GitHub. Set `repo_config` per test."""
    repo_config: str | None = None
    posted = None
    checks: list = []
    existing_comments: list = []

    def list_review_comments(self, repo, number):
        return FakeGitHub.existing_comments

    def __init__(self, settings, installation_id):
        FakeGitHub.checks = []

    def list_pr_files(self, repo, number):
        return [
            {"filename": "app/run.py", "status": "added", "patch": PATCH},
            {"filename": "poetry.lock", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ]

    def get_file_content(self, repo, path, ref):
        return FakeGitHub.repo_config if path == ".ai-review.yml" else CONTENT

    def create_review(self, repo, number, commit_id, body, comments):
        FakeGitHub.posted = {"body": body, "comments": comments, "commit_id": commit_id}
        return {"id": 99}

    def start_check(self, repo, sha):
        FakeGitHub.checks.append(("start", sha))
        return 555

    def finish_check(self, repo, sha, check_id, conclusion, title, summary):
        FakeGitHub.checks.append(("finish", check_id, conclusion, title))

    def close(self):
        pass


class FakeLLM:
    seen_instructions = None

    def __init__(self, settings):
        FakeLLM.seen_instructions = settings.review_instructions

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


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(review_service, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(review_service, "LLMReviewer", FakeLLM)
    FakeGitHub.repo_config = None
    FakeGitHub.posted = None
    FakeGitHub.existing_comments = []
    return Settings(github_webhook_secret="s", github_token="t")


def run(settings):
    return review_service.review_pull_request(settings, "octo/repo", 1, "sha1", "t", "b", None)


def test_full_review_pipeline(fakes):
    result = run(fakes)

    assert result["status"] == "posted"
    comments = FakeGitHub.posted["comments"]
    assert [(c["line"], c["side"]) for c in comments] == [(2, "RIGHT"), (1, "RIGHT")]  # high before medium
    assert "static analysis" in comments[0]["body"]
    assert "```suggestion\ndef run(cmd: str):\n```" in comments[1]["body"]
    assert "Looks risky." in FakeGitHub.posted["body"]
    assert FakeGitHub.posted["commit_id"] == "sha1"
    # default fail_on=critical: a high finding still passes
    assert FakeGitHub.checks[0] == ("start", "sha1")
    assert FakeGitHub.checks[-1][:3] == ("finish", 555, "success")


def test_repo_config_overrides(fakes):
    FakeGitHub.repo_config = "fail_on: high\nmin_severity: high\ninstructions: Prefer pathlib.\n"
    result = run(fakes)
    assert len(FakeGitHub.posted["comments"]) == 1  # medium comment filtered out
    assert FakeLLM.seen_instructions == "Prefer pathlib."
    assert result["check"] == "failure"
    assert FakeGitHub.checks[-1][3].startswith("1 issue(s) at or above high")


def test_repo_config_can_disable_reviews(fakes):
    FakeGitHub.repo_config = "enabled: false\n"
    assert run(fakes) == {"status": "skipped", "reason": "disabled by .ai-review.yml"}
    assert FakeGitHub.posted is None and FakeGitHub.checks == []


def test_invalid_repo_config_warns_and_uses_defaults(fakes):
    FakeGitHub.repo_config = "max_comments: lots\n"
    assert run(fakes)["status"] == "posted"
    assert "`.ai-review.yml` is invalid" in FakeGitHub.posted["body"]


def test_failure_marks_check_neutral(fakes, monkeypatch):
    monkeypatch.setattr(FakeGitHub, "list_pr_files", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run(fakes)
    assert FakeGitHub.checks[-1][:3] == ("finish", 555, "neutral")


def test_rereview_skips_already_posted_issues(fakes):
    run(fakes)
    first = FakeGitHub.posted["comments"]
    assert all("<!-- ai-review:" in c["body"] for c in first)

    # Second review of the same code: GitHub now returns the first review's comments
    FakeGitHub.existing_comments = first
    result = run(fakes)
    assert result["comments"] == 0
    assert FakeGitHub.posted["comments"] == []
    assert "**2** issue(s) reported earlier are still present" in FakeGitHub.posted["body"]
    # The check still reflects every current issue, not just new ones
    assert FakeGitHub.checks[-1][3] == "1 high, 1 medium"


def test_rereview_recognises_comments_from_before_fingerprints(fakes):
    legacy = {"path": "app/run.py", "line": 2,
              "body": "🔴 **Use of `eval()`** · `high` · _security_ · static analysis\n\nold body"}
    FakeGitHub.existing_comments = [legacy]
    run(fakes)
    assert [c["line"] for c in FakeGitHub.posted["comments"]] == [1]  # only the new medium issue


def test_fingerprint_survives_line_shift():
    a = Finding(path="a.py", line=3, severity=Severity.high, category="security", title="t", body="b")
    b = a.model_copy(update={"line": 9, "title": "reworded by the LLM"})
    assert review_service.fingerprint(a, "  x = eval(s)") == review_service.fingerprint(b, "x = eval(s)")
    assert review_service.fingerprint(a, "x = eval(s)") != review_service.fingerprint(a, "y = eval(s)")


def test_min_severity_filter():
    settings = Settings(github_webhook_secret="s", github_token="t", min_severity="high")
    files = [FileDiff(path="a.py", status="added", patch="", commentable_lines={1, 2})]
    low = Finding(path="a.py", line=1, severity=Severity.low, category="style", title="t", body="b")
    crit = Finding(path="a.py", line=2, severity=Severity.critical, category="security", title="t", body="b")
    assert review_service.merge_findings(files, [low], [crit], settings) == [crit]


@pytest.mark.parametrize("fail_on,expected", [("critical", "success"), ("medium", "failure"), ("never", "success")])
def test_check_conclusion(fail_on, expected):
    findings = [Finding(path="a.py", line=1, severity=Severity.high, category="bug", title="t", body="b")]
    assert review_service.check_conclusion(findings, fail_on)[0] == expected
    assert review_service.check_conclusion([], fail_on) == ("success", "No issues found")
