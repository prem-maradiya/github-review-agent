# 🤖 AI Code Review Agent for GitHub

An agent that reviews pull requests on its own. It listens for GitHub webhooks, runs **AST-based static analysis** and an **LLM review (Claude)** on each PR, and posts one review with inline comments on the changed lines.

**Stack:** FastAPI · GitHub Webhooks · Celery · Redis · Claude API (structured outputs) · Python `ast` · Docker

## Architecture

```
GitHub PR event ──► FastAPI /webhook/github
                      │  1. verify HMAC-SHA256 signature
                      │  2. filter (opened/synchronize/reopened/ready_for_review, non-draft)
                      │  3. dedupe delivery ID in Redis (SET NX)
                      │  4. record latest head SHA in Redis
                      ▼
                  Redis (broker) ──► Celery worker
                                       │  per-PR Redis lock, drops stale commits
                                       │  fetch PR files + patches (GitHub API)
                                       │  filter lockfiles/binaries, enforce size budgets
                                       │  AST analysis on changed .py files (changed lines only)
                                       │  Claude review on a line-numbered diff + AST hints
                                       │  validate lines against the diff, dedupe, rank, cap
                                       ▼
                                  POST /pulls/{n}/reviews  (one review with inline comments)
```

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI webhook receiver, `/health`, `/tasks/{id}` |
| `app/security.py` | Constant-time webhook signature check |
| `app/tasks.py` / `celery_app.py` | Async job with retries/backoff, per-PR lock, stale-commit skip |
| `app/github_client.py` | GitHub App (JWT → installation token) or PAT auth; files, contents, reviews |
| `app/diff_parser.py` | Hunk parsing → commentable/added line numbers; line-numbered patch for the LLM |
| `app/ast_analyzer.py` | Static checks (see below) |
| `app/llm_reviewer.py` | Claude call using `messages.parse` + a Pydantic schema |
| `app/review_service.py` | Pipeline orchestration, merging, formatting |

### AST checks
Hardcoded secrets · `eval`/`exec` · `shell=True` / `os.system` · `pickle`/unsafe `yaml.load` · `verify=False` · bare `except:` · swallowed exceptions · mutable default args · cyclomatic complexity > 10 · functions > 60 lines · > 6 params · `== None` · wildcard imports · unused imports · syntax errors.

Findings only count when they touch lines the PR **added**, so the bot doesn't flag old code.

### LLM review
- The diff is sent with new-file line numbers (`  42 +code`), so the model cites lines GitHub will accept.
- The output is checked against a Pydantic schema (`LLMReview`), so there's no fragile JSON parsing.
- AST findings go in as hints, so the model skips mechanical issues and looks for logic, security and design problems.
- PR content is treated as untrusted data in the system prompt, as a defence against prompt injection.
- Server-side refusal fallback is on (`fallbacks="default"`). If a safety classifier declines, the API retries on a fallback model in the same call.
- Any comment whose line isn't in the diff gets dropped. If GitHub still rejects the inline comments (422), the review is posted as summary-only.

## Setup

### 1. Configure
```bash
cp .env.example .env
```
Fill in `GITHUB_WEBHOOK_SECRET`, `ANTHROPIC_API_KEY`, and one of:
- **GitHub App** (recommended): `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_PATH`. App permissions: *Pull requests: Read & write*, *Contents: Read*. Subscribe to the **Pull request** event.
- **PAT**: `GITHUB_TOKEN` (fine-grained, with the same permissions).

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
- Events: **Pull requests**

Open a PR and the review appears within a minute or so.

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
The tests cover diff parsing, AST checks, signature verification, webhook filtering and dedupe (with fakeredis), and the full review pipeline with fake GitHub and LLM clients.

## Configuration reference
| Var | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `claude-opus-5-5` | Claude model |
| `LLM_EFFORT` | `high` | `low`–`max`; lower is cheaper and faster |
| `LLM_ENABLED` | `true` | `false` = AST-only reviews |
| `MAX_COMMENTS` | `25` | Cap on inline comments per review |
| `MIN_SEVERITY` | `low` | Drop findings below this level |
| `MAX_FILES` / `MAX_TOTAL_PATCH_CHARS` | `50` / `150000` | Size budgets; larger files are listed as skipped |

## Possible extensions
- `/review` slash command via `issue_comment` events
- Chunk very large PRs into several LLM calls
- AST analyzers for other languages (tree-sitter)
- Per-repo config file (`.ai-review.yml`)
