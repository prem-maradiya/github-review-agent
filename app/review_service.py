"""Orchestrates a full PR review: fetch -> filter -> AST -> LLM -> merge -> post."""
from __future__ import annotations

import fnmatch
import logging
from collections import Counter

from app.ast_analyzer import analyze_python
from app.config import Settings
from app.diff_parser import added_lines, commentable_lines
from app.github_client import GitHubClient
from app.llm_reviewer import LLMReviewer
from app.models import SEVERITY_EMOJI, SEVERITY_RANK, FileDiff, Finding, Severity
from app.repo_config import CONFIG_PATH, apply_repo_config, parse_repo_config

log = logging.getLogger(__name__)
MARKER = "<!-- ai-code-review-agent -->"


def is_ignored(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], p) for p in patterns)


def select_files(raw_files: list[dict], settings: Settings) -> tuple[list[FileDiff], list[str]]:
    """Pick reviewable files within size budgets. Returns (files, skipped_paths)."""
    selected: list[FileDiff] = []
    skipped: list[str] = []
    budget = settings.max_total_patch_chars
    for f in raw_files:
        path, patch = f["filename"], f.get("patch")
        if f.get("status") == "removed" or not patch or is_ignored(path, settings.ignored_paths):
            continue
        if len(patch) > settings.max_patch_chars_per_file or len(patch) > budget or len(selected) >= settings.max_files:
            skipped.append(path)
            continue
        budget -= len(patch)
        selected.append(FileDiff(path=path, status=f["status"], patch=patch, commentable_lines=commentable_lines(patch)))
    return selected, skipped


def merge_findings(files: list[FileDiff], ast_findings: list[Finding], llm_findings: list[Finding],
                   settings: Settings) -> list[Finding]:
    allowed = {f.path: f.commentable_lines for f in files}
    min_rank = SEVERITY_RANK[Severity(settings.min_severity)]
    seen: set[tuple[str, int, str]] = set()
    merged: list[Finding] = []
    for f in ast_findings + llm_findings:  # AST first: wins ties on the same line
        if f.line not in allowed.get(f.path, set()):
            log.debug("Dropping finding on non-diff line %s:%s", f.path, f.line)
            continue
        if SEVERITY_RANK[f.severity] < min_rank:
            continue
        key = (f.path, f.line, f.category)
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)
    merged.sort(key=lambda f: (-SEVERITY_RANK[f.severity], f.path, f.line))
    return merged[: settings.max_comments]


def format_comment(f: Finding) -> str:
    tag = "static analysis" if f.source == "ast" else "AI review"
    body = f"{SEVERITY_EMOJI[f.severity]} **{f.title}** · `{f.severity.value}` · _{f.category}_ · {tag}\n\n{f.body}"
    if f.suggestion.strip():
        body += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
    return body


def check_conclusion(findings: list[Finding], fail_on: str) -> tuple[str, str]:
    """Decide the PR check result. Returns (conclusion, one-line title)."""
    counts = Counter(f.severity for f in findings)
    parts = [f"{counts[s]} {s.value}" for s in sorted(counts, key=lambda s: -SEVERITY_RANK[s])]
    title = ", ".join(parts) if parts else "No issues found"
    if fail_on != "never":
        threshold = SEVERITY_RANK[Severity(fail_on)]
        blocking = sum(n for s, n in counts.items() if SEVERITY_RANK[s] >= threshold)
        if blocking:
            return "failure", f"{blocking} issue(s) at or above {fail_on}: {title}"
    return "success", title


def format_summary(llm_summary: str, findings: list[Finding], files: list[FileDiff], skipped: list[str],
                   config_warning: str | None = None) -> str:
    counts = Counter(f.severity for f in findings)
    lines = [MARKER, "## 🤖 AI Code Review", ""]
    if config_warning:
        lines += [f"> ⚠️ {config_warning}", ""]
    lines += [llm_summary or "_No AI summary._", ""]
    lines.append(f"Reviewed **{len(files)}** file(s), left **{len(findings)}** comment(s).")
    if findings:
        lines += ["", "| Severity | Count |", "|---|---|"]
        for sev in sorted(counts, key=lambda s: -SEVERITY_RANK[s]):
            lines.append(f"| {SEVERITY_EMOJI[sev]} {sev.value} | {counts[sev]} |")
    if skipped:
        lines += ["", f"<details><summary>Skipped {len(skipped)} large file(s)</summary>\n"]
        lines += [f"- `{p}`" for p in skipped] + ["</details>"]
    return "\n".join(lines)


def review_pull_request(settings: Settings, repo: str, number: int, head_sha: str,
                        title: str, body: str, installation_id: int | None) -> dict:
    gh = GitHubClient(settings, installation_id)
    check_id, check_started = None, False
    try:
        # 0) Per-repo overrides from .ai-review.yml at the PR head
        cfg, config_warning = parse_repo_config(gh.get_file_content(repo, CONFIG_PATH, head_sha))
        if not cfg.enabled:
            log.info("Reviews disabled by %s in %s", CONFIG_PATH, repo)
            return {"status": "skipped", "reason": f"disabled by {CONFIG_PATH}"}
        settings = apply_repo_config(settings, cfg)

        check_id, check_started = gh.start_check(repo, head_sha), True
        files, skipped = select_files(gh.list_pr_files(repo, number), settings)
        if not files:
            log.info("No reviewable files in %s#%s", repo, number)
            gh.finish_check(repo, head_sha, check_id, "success", "No reviewable files", "Nothing to review.")
            return {"status": "skipped", "reason": "no reviewable files"}

        # 1) Deterministic AST analysis on changed Python files
        ast_findings: list[Finding] = []
        for f in files:
            if f.path.endswith(".py"):
                f.content = gh.get_file_content(repo, f.path, head_sha)
                if f.content:
                    ast_findings += analyze_python(f.path, f.content, added_lines(f.patch))

        # 2) LLM review with AST findings as hints
        llm_summary, llm_findings = "", []
        if settings.llm_enabled:
            llm_summary, llm_findings = LLMReviewer(settings).review(title, body, files, ast_findings)

        # 3) Merge, dedupe, rank, and post a single review
        findings = merge_findings(files, ast_findings, llm_findings, settings)
        comments = [{"path": f.path, "line": f.line, "side": "RIGHT", "body": format_comment(f)} for f in findings]
        summary = format_summary(llm_summary, findings, files, skipped, config_warning)
        review = gh.create_review(repo, number, head_sha, summary, comments)
        log.info("Posted review %s on %s#%s with %d comments", review.get("id"), repo, number, len(comments))

        # 4) Report a pass/fail check on the PR
        conclusion, check_title = check_conclusion(findings, settings.fail_on)
        gh.finish_check(repo, head_sha, check_id, conclusion, check_title, summary)
        return {"status": "posted", "review_id": review.get("id"), "comments": len(comments),
                "ast_findings": len(ast_findings), "llm_findings": len(llm_findings), "check": conclusion}
    except Exception as e:
        if check_started:
            gh.finish_check(repo, head_sha, check_id, "neutral", "Review failed", f"Error: {type(e).__name__}")
        raise
    finally:
        gh.close()
