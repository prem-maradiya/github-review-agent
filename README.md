# 🤖 AI Code Review Agent for GitHub

[![CI](https://github.com/prem-maradiya/github-review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/prem-maradiya/github-review-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)

An agent that reviews pull requests on its own. It listens for GitHub webhooks, runs **AST-based static analysis** and an **LLM review (Google Gemini, free tier)** on each PR, and posts one review with inline comments on the changed lines.

**Stack:** FastAPI · GitHub Webhooks · Celery · Redis · Google Gemini API (structured outputs) · Python `ast` · Docker · GitHub Actions

## Features
- **Automatic reviews** whenever a PR is opened or new commits are pushed
- **`/review` command:** comment it on any PR to re-run the review (owners, members and collaborators only)
- **Pass/fail PR check:** reported as a check run (GitHub App) or a commit status (token), and can block merging
- **Per-repo config:** `.ai-review.yml` sets severity, ignored paths, the fail threshold and custom reviewer instructions
- **Static and AI review together:** fast, deterministic AST checks plus Gemini for logic and security issues
- **Built to be reliable:** webhook signature checks, delivery dedupe, per-PR locks, stale-commit skipping, retries with backoff and model fallback

## Example output
This is real output from the agent on a test diff (a user lookup with a string-built SQL query):

> ## 🤖 AI Code Review
> The PR introduces a user lookup helper and a numeric utility function. However, `get_user` contains a critical SQL injection vulnerability and risks raising an `IndexError` when a user is not found. Additionally, `average` is vulnerable to a `ZeroDivisionError` when passed an empty list.
>
> Reviewed **1** file(s), left **4** comment(s).
>
> | Severity | Count |
> |---|---|
> | 🚨 critical | 1 |
> | 🔴 high | 1 |
> | ⚠️ medium | 1 |
> | 💡 low | 1 |

Inline comments, one per line:

| Line | | Finding | Source |
|---|---|---|---|
| 5 | 🚨 | SQL Injection vulnerability | AI review |
| 7 | 🔴 | IndexError when user is not found | AI review |
| 10 | ⚠️ | Potential ZeroDivisionError on empty input | AI review |
| 1 | 💡 | Unused import | static analysis |

The PR check then reads **"AI Code Review: 1 issue(s) at or above critical"** and fails.

## Architecture

```
GitHub PR event / "/review" ──► FastAPI /webhook/github
                      │  1. verify HMAC-SHA256 signature
                      │  2. filter (opened/synchronize/reopened/ready_for_review, non-draft;
                      │     or a /review comment from a trusted user)
                      │  3. dedupe delivery ID in Redis (SET NX)
                      │  4. record latest head SHA in Redis
                      ▼
                  Redis (broker) ──► Celery worker
                                       │  per-PR Redis lock, drops stale commits
                                       │  load .ai-review.yml from the PR head
                                       │  mark PR check "in progress"
                                       │  fetch PR files + patches (GitHub API)
                                       │  filter lockfiles/binaries, enforce size budgets
                                       │  AST analysis on changed .py files (changed lines only)
                                       │  Gemini review on a line-numbered diff + AST hints
                                       │  validate lines against the diff, dedupe, rank, cap
                                       ▼
                                  POST /pulls/{n}/reviews  (one review with inline comments)
                                  PR check → success / failure (based on fail_on)
```

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI webhook receiver, `/health`, `/tasks/{id}` |
| `app/security.py` | Constant-time webhook signature check |
| `app/tasks.py` / `celery_app.py` | Async job with retries/backoff, per-PR lock, stale-commit skip |
| `app/github_client.py` | GitHub App (JWT → installation token) or PAT auth; files, contents, reviews, checks/statuses |
| `app/repo_config.py` | Parses and validates `.ai-review.yml` and applies per-repo overrides |
| `app/diff_parser.py` | Hunk parsing → commentable/added line numbers; line-numbered patch for the LLM |
| `app/ast_analyzer.py` | Static checks (see below) |
| `app/llm_reviewer.py` | Gemini call with a Pydantic `response_schema`, model fallback |
| `app/review_service.py` | Pipeline orchestration, merging, formatting |

### AST checks
Hardcoded secrets · `eval`/`exec` · `shell=True` / `os.system` · `pickle`/unsafe `yaml.load` · `verify=False` · bare `except:` · swallowed exceptions · mutable default args · cyclomatic complexity > 10 · functions > 60 lines · > 6 params · `== None` · wildcard imports · unused imports · syntax errors.

Findings only count when they touch lines the PR **added**, so the bot doesn't flag old code.

### LLM review
> On Gemini's free tier, Google may use prompts to improve its products. Use a paid key for private or company code.

- The diff is sent with new-file line numbers (`  42 +code`), so the model cites lines GitHub will accept.
- The output is checked against a Pydantic schema (`LLMReview`), so there's no fragile JSON parsing.
- AST findings go in as hints, so the model skips mechanical issues and looks for logic, security and design problems.
- PR content is treated as untrusted data in the system prompt, as a defence against prompt injection.
- If the main model is overloaded or rate-limited (429/5xx), it tries the next model in `LLM_FALLBACK_MODELS`. If all are busy, Celery retries the job with backoff.
- Any comment whose line isn't in the diff gets dropped. If GitHub still rejects the inline comments (422), the review is posted as summary-only.

## Setup

### 1. Configure
```bash
cp .env.example .env
```
Fill in `GITHUB_WEBHOOK_SECRET`, `GEMINI_API_KEY` (free from https://aistudio.google.com/apikey), and one of:
- **GitHub App** (recommended): `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_PATH`.
  - App permissions: *Pull requests: Read & write*, *Contents: Read*, *Checks: Read & write*, *Issues: Read & write* (for 👀 reactions on `/review`).
  - Subscribe to the **Pull request** and **Issue comment** events.
- **PAT**: `GITHUB_TOKEN`, fine-grained, with *Pull requests: Read & write*, *Contents: Read*, *Commit statuses: Read & write* and *Issues: Read & write*. The Checks API only works with GitHub Apps, so a PAT reports a commit status instead.

### 2. Run with Docker
```bash
docker compose up --build
```
This starts `api` (port 8000), `worker` (Celery) and `redis`.

### 3. Expose the webhook (local dev)
```bash
ngrok http 8000
```
In the repo or App settings, go to **Webhooks** and set:
- Payload URL: `https://<ngrok-id>.ngrok.app/webhook/github`
- Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET`
- Events: **Pull requests** and **Issue comments**

Open a PR and the review appears within a minute or so. To re-run it, comment `/review` on the PR.

To block merging on serious issues, go to **Settings → Branches → branch protection** and mark **AI Code Review** as a required check.

## Per-repo config (`.ai-review.yml`)
Put this file in the root of any repo the agent reviews. It's read from the PR's head commit. Every key is optional; see [`.ai-review.example.yml`](.ai-review.example.yml).

```yaml
min_severity: medium      # drop findings below this level
max_comments: 15
fail_on: high             # PR check fails at/above this severity, or "never"
ignored_paths: ["docs/*", "migrations/*"]
instructions: |
  We use SQLAlchemy 2.0; flag any raw SQL string building.
```
Setting `enabled: false` turns reviews off for that repo. If the file is invalid, the agent uses the defaults and says so in the review.

### Run without Docker
```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements-dev.txt
redis-server &   # or: docker run -p 6379:6379 redis:7-alpine
uvicorn app.main:app --reload
celery -A app.celery_app worker --loglevel=info --pool=solo   # --pool=solo on Windows
```

## Tests
```bash
pytest -q
```
The tests cover:
- diff parsing and AST checks
- signature verification, webhook filtering and dedupe (with fakeredis)
- the `/review` command and its permission checks
- `.ai-review.yml` parsing and overrides
- PR check results
- the full review pipeline, with fake GitHub and LLM clients

GitHub Actions runs them on every push and PR. It also builds the Docker image and checks that the API starts.

## Configuration reference
| Var | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `gemini-3.5-flash` | Main Gemini model |
| `LLM_FALLBACK_MODELS` | `["gemini-2.5-flash","gemini-3.5-flash-lite"]` | Tried in order when the main model is busy |
| `LLM_ENABLED` | `true` | `false` = AST-only reviews |
| `MAX_COMMENTS` | `25` | Cap on inline comments per review |
| `MIN_SEVERITY` | `low` | Drop findings below this level |
| `FAIL_ON` | `critical` | PR check fails at/above this severity (`never` = always pass) |
| `REVIEW_COMMAND` | `/review` | Comment that triggers a re-review |
| `MAX_FILES` / `MAX_TOTAL_PATCH_CHARS` | `50` / `150000` | Size budgets; larger files are listed as skipped |

## Roadmap
- Split very large PRs into several LLM calls
- Incremental reviews (only new commits, no repeated comments)
- AST analyzers for other languages (tree-sitter)
- Merge in Ruff, Bandit and Semgrep findings
- Dashboard of reviewed PRs and token usage
