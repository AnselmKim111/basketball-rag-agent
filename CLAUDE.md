# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project identity

The repo is named `basketball-rag-agent`, but the actual project is **`wisereport-auto-downloader`** — a Korean financial-report scraper + LLM summarizer + Telegram bot stack. Ignore the repo name; the code, `pyproject.toml`, and Telegram bot UX are all about Korean equity research from `wisereport.co.kr` (and DART for the deepdive feature). All user-facing text (Telegram messages, prompt templates, log messages) is in **Korean** — preserve Korean strings verbatim when editing.

## Common commands

```bash
# Local CLI run (one-shot: download + summarize + Telegram-send for one company)
python -m src.main 삼성전자 --ticker 005930
python -m src.main "SK하이닉스" --ticker 000660 --top 5
python -m src.main 카카오 --ticker 035720 --no-summarize --no-telegram

# Run a single bot locally (just CompanyBot polling)
python -m src.bot_worker

# Run the full 4-bot orchestrator (this is what Docker CMD runs)
python -m src.orchestrator

# Container build/run (this is the Railway deploy target)
docker build -t wisereport .
docker run --env-file .env wisereport
```

There are **no tests, lints, or formatters configured**. Don't claim a task is verified by "running tests" — the only verification is running one of the entry points above against real wisereport credentials, or reading the diagnostic env-var dump that `orchestrator._diag_env()` and `bot_worker.main()` print at startup.

## Dependency files: `requirements.txt` vs `pyproject.toml`

These two files **disagree**, and `requirements.txt` is the source of truth.

- `requirements.txt` (Docker uses this) lists `openai`, `python-telegram-bot`, `APScheduler`, `matplotlib`, `beautifulsoup4`, `lxml`, etc. — the actual runtime stack.
- `pyproject.toml` still lists `anthropic` and is missing most of the runtime deps. The `import anthropic` was removed in commit `c4ff262`; the metadata file just hasn't caught up. Don't add code that imports `anthropic` — the codebase uses the **OpenAI SDK pointed at OpenRouter** (see `src/summarizer.py:get_client`).

When adding deps: edit `requirements.txt`. Touch `pyproject.toml` only if you also intend to fix the project metadata.

## Architecture

### One Playwright session, four bots, one lock

`src/orchestrator.py` builds up to 4 Telegram `Application`s on a single asyncio loop, each gated by the presence of its token:

| Bot | Token env | Trigger |
| --- | --- | --- |
| CompanyBot | `TELEGRAM_BOT_TOKEN` | On-demand: `/report <name> [ticker] [top]`, plus `/deepdive` (DART) |
| IndustryBot | `INDUSTRY_BOT_TOKEN` | Cron Mon/Wed/Fri 09:00 KST + on-demand industry name |
| MarketBot | `MARKET_BOT_TOKEN` | Cron daily 09:00 KST (new strategy reports only) |
| GlobalBot | `GLOBAL_BOT_TOKEN` | Cron Sat 09:00 KST |

All bots share **one `asyncio.Lock` in `src/pipeline_lock.py` (`PIPELINE_LOCK`)**. This is critical: every wisereport pipeline (Playwright login + AJAX scrape + PDF download) acquires this lock before running. Wisereport's duplicate-login detection nukes a session if two flows log in concurrently with the same credentials, so serialization isn't a perf concession — it's correctness. When adding any new wisereport-touching code path, wrap it in `async with PIPELINE_LOCK:`.

Schedules (APScheduler `AsyncIOScheduler`) are wired in `orchestrator.py` with `CronTrigger` in KST timezone (`misfire_grace_time=3600`, `coalesce=True`, `max_instances=1`).

### CompanyBot fan-out via subprocess

`src/bot_worker.py:_run_pipeline` does **not** call `src.main` in-process — it spawns it as a subprocess (`asyncio.create_subprocess_exec(sys.executable, "-m", "src.main", ...)`). The category bots (`src/category_bots.py:_process_and_send_category`) instead run their blocking pipeline in a thread executor (`loop.run_in_executor`). Both styles are intentional:

- The subprocess gives the CompanyBot crash isolation — if Playwright OOMs or hangs, only that subprocess dies.
- The category bots keep their work in-process so they can re-use the OpenRouter client and get back per-step exception types.

Both still acquire `PIPELINE_LOCK` first, so only one wisereport session ever runs at a time across all bots.

### Wisereport scraping (`src/wisereport.py`)

`WisereportClient` is a `with`-context Playwright wrapper. Notable shape:

- Login goes through a `placeholder swap` trick on `#UsrPassWD` — direct `page.fill()` lands the password in the wrong field, so login is done by `page.evaluate` setting jQuery `.val()` and calling `CheckVal()`. Don't "simplify" this back to `page.fill`.
- The client uses a short, **plain UA string**. Including `Win64; x64` or `AppleWebKit` in the UA changes which JS path the site takes and breaks login form submission.
- Sessions are cached to `STORAGE_STATE` (default `./.wisereport_state.json`). `ensure_logged_in()` re-uses the file if present and only re-logs in if `ReportList` redirects to `returnUrl`. **Do not pre-flight `ReportList` before a fresh login** — it dirties the cookies and breaks the subsequent login (see comment in `ensure_logged_in`).
- Two HTML response shapes are parsed with regex:
  - `gotoSearchContent(rpt_id, sch_db, sch_lang, sch_dt, sch_gubun, sch_cont, …)` for the per-ticker `contentList.aspx` POST.
  - `gotoSearch(rpt_id, sch_db, sch_lang, sch_dt, sch_cont)` for the TopHits `topHits_*.aspx` POSTs.
  These regexes assume single-quoted args in that exact order. If wisereport changes its markup, both will break silently and return zero items.
- PDF download is two-step: GET `LoadReportBody.aspx` → regex `openContent('rpt_id','brk_cd','fpath',…)` → GET `LoadReport.aspx` triggers a Playwright `expect_download`.

### Summarization (`src/summarizer.py`)

OpenRouter via the OpenAI-compatible SDK. Two prompt sizes:

- `summarize_pdf` → 5000-char Korean summary with a fixed 6-section template (system prompt at top of file).
- `summarize_pdf_short` → 1000-char single-paragraph summary (used by category bots).

Both wrap `APIStatusError` and detect credit-exhaustion via status 402/429 plus body keyword (`credit|balance|payment|insufficient|quota`) — they raise `OpenRouterCreditExhausted`. The CLI catches this and falls back to PDF-only Telegram delivery; the category bots stuff a `(요약 실패: ...)` placeholder in-line and keep going.

PDFs are read with `pypdf` and capped at `MAX_PDF_TEXT_CHARS = 80_000` (60_000 for the short variant). Anything beyond is annotated `(이후 N페이지 생략)`. Don't chase per-page cleverness — the project deliberately keeps text-extraction simple.

### Deepdive feature (`src/deepdive/`)

`/deepdive` on CompanyBot. **Intentionally isolated** — `bot_worker.build_company_app` calls `register(app)` inside a `try/except`, and `src/deepdive/__init__.py` is empty so importing the package is side-effect free. Heavy deps (`matplotlib`, anything DART-related) are imported only inside functions. Maintain this pattern when adding to deepdive — a deepdive failure must never break the other bots.

Two kill switches:

- `DART_API_KEY` missing → handler not registered.
- `DEEPDIVE_ENABLED=0` → handler not registered.

Pipeline (`handler._execute`):

1. DART corp_code lookup
2. Latest business report → "업의 본질" 1000-char summary
3. wisereport context (3 company + 1 industry reports) collected via `wisereport_context.collect_for_ticker` and injected as `extra_context` into the LLM call
4. Latest IR PDF (or wisereport-only fallback) → "핵심 투자 포인트" summary
5. Quarterly financials (`fetch_quarterly_financials`) + preliminary-quarter augmentation + forward consensus (LLM extracts forward numbers from wisereport company reports → `forward_consensus.from_wisereport`) → matplotlib chart PNG
6. Original business-report PDF send

Every step is wrapped in `try/except` that logs and posts a `⚠️` message to Telegram but never raises out of `_execute`.

System prompts for the deepdive LLM calls live in **editable text files** under `prompts/` (`deepdive_business.txt`, `deepdive_ir.txt`, `deepdive_ir_fallback.txt`). `src/deepdive/prompts.py:load` reads them on every call (no caching) and falls back to short hard-coded strings if the file is missing. The Dockerfile copies `prompts/` into the image, so editing a prompt and pushing redeploys with the new prompt active.

### Dedup state (`src/state_store.py`)

JSON file `seen_rpt_ids.json` of `{category: [rpt_id, ...]}`, capped at 1000 entries per category. Storage path lookup priority: `RAILWAY_VOLUME_MOUNT_PATH` → `STATE_DIR` → `/data` → `/app/data` → `/tmp/wisereport_state` → `/tmp`. Without a Railway volume mount, the state vanishes on container restart and a few reports may be re-sent — this is intentional (acceptable tradeoff for not requiring a DB).

The category jobs that use dedup pass `dedup_key=` to `_process_and_send_category`. The dedup keys in use are `"industry_top10"`, `"strategy_daily"`, `"global_top10"`. The on-demand industry path passes `dedup_key=None` — every manual request re-fetches.

## Authorization model

Every Telegram handler short-circuits if the chat ID is not in the bot's allowlist env var:

- CompanyBot: `ALLOWED_CHAT_IDS`
- IndustryBot: `INDUSTRY_ALLOWED_CHAT_IDS`
- MarketBot: `MARKET_ALLOWED_CHAT_IDS`
- GlobalBot: `GLOBAL_ALLOWED_CHAT_IDS`

These are comma-separated. Unauthorized users get a one-shot reply showing their own `chat_id` (so the operator can add them) and then nothing else. When adding handlers, copy the `_is_authorized` / `is_authorized` pattern from the existing handlers — never rely on Telegram username.

## Diagnostic startup dumps

Both `orchestrator._diag_env()` and `bot_worker.main()` print a sanitized list of environment variables related to TELEGRAM/WISE/OPEN/CHAT/INDUSTRY/MARKET/GLOBAL on startup, with token-length placeholders for sensitive ones. When debugging Railway deploys (the most common operational issue), grep the container logs for `[orch] relevant env vars` or `DIAG: 관련 env vars`.
