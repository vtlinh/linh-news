# scripts/

One-off helpers — setup tools, dev re-render shortcuts, and the local cron
entry point. None of these run in production: the Fly app only serves
the FastAPI viewer, and the generation pipeline fires from Windows Task
Scheduler on Linh's local machine via `trigger_cron.ps1`.

All commands assume the repo root as CWD and a running Fly Postgres proxy:

```bash
# In a separate terminal (leave running):
fly proxy 15432:5432 -a linh-news-db
```

---

## `trigger_cron.ps1` — local smart cron

The actual production-equivalent trigger. Windows Task Scheduler fires
this every 6 hours; it runs `uv run python -m app.generate <slot> --smart`
locally so WeasyPrint has plenty of RAM and the small Fly VM doesn't
need to OOM-juggle. The `--smart` path consults `decide_cron_action()`
per user (skip if already succeeded today, retry post-LLM stages if a
downstream step failed, etc).

Not invoked manually under normal circumstances.

## `google_oauth_setup.py` — mint a Google refresh token

One-shot, run locally:

```bash
uv run python scripts/google_oauth_setup.py
```

Walks through the standard installed-app OAuth consent flow in your
browser, then writes `(client_id, client_secret, refresh_token)` into
the `google_oauth` table so production can fetch calendar events
non-interactively. Re-run only when the refresh token is revoked
(see also commit `68f71fd` for the auto-detection).

## `seed_admin_settings.py` — seed admin's `user_settings` row

```bash
uv run python -m scripts.seed_admin_settings
```

Idempotent. Writes a default `user_settings` row for the admin email
based on `news.pr`'s section defaults. Run once after migration `0019`
or any time you want to reset the admin's section toggles to defaults.

## `copy_pg_to_sqlite.py` — clone Fly Postgres into local SQLite

```bash
fly proxy 15432:5432 -a linh-news-db          # separate terminal
uv run alembic upgrade head                    # creates SQLite schema
uv run python scripts/copy_pg_to_sqlite.py
```

Truncates each target table on the SQLite side before inserting, so
this is safe to re-run for a fresh local snapshot. Useful for offline
debugging against real production data.

## `rerender_today.py` — re-run the post-LLM pipeline (no LLM call)

```bash
uv run python -m scripts.rerender_today --email vtlinh87@gmail.com
uv run python -m scripts.rerender_today --email vtlinh87@gmail.com --date 2026-05-13
```

Loads `editions.content_json` for `(date, email)` and pipes it back
through `generate.run(linhnews_override=...)`. Image fetch, calendar,
weather, HTML render, and WeasyPrint all execute — but no Anthropic
call is made. The path most useful for iterating on PDF/HTML layout
without burning LLM credits.

The previous AI cost stored on `content_json._ai_cost_usd` is preserved
in the dateline (re-renders show the original cost, not $0.00).

Prints the latest `logs/pdf-refresh-*.pdf` and the assembled HTML
snapshot path on success.

## `rerender_pdf.py` — re-render PDF from stored `pdf_html`

```bash
uv run python scripts/rerender_pdf.py
```

Skips the structured-render path entirely: takes `Edition.pdf_html` as
input and pipes it straight to WeasyPrint. The verbose URL fetcher
logs every image fetch attempt — handy for tracking down missing
images or overflow without rebuilding the document.

## `rebuild_pdf_from_html.py` — rebuild PDF from `content_json`

```bash
uv run python scripts/rebuild_pdf_from_html.py
```

Like `rerender_pdf.py` but goes through `app.pdf_renderer.build_pdf_html`
so any renderer changes (typography, layout chrome, fit logic) actually
take effect. Re-fetches calendar / movies / weather server-side and
updates `Edition.pdf` + `Edition.pdf_html` in place.
