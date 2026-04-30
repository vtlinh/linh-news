# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Twice-daily generator of a personalized news edition for Linh:
- An **HTML page** (sources, hide buttons, calendar, weather, stocks, movies for kids 8–12)
- A one-page **"Linh Times" PDF** styled like the New York Times front page

Architecture and decisions are captured in the approved plan at `~/.claude/plans/let-s-create-a-news-pr-expressive-pinwheel.md`. Read it before making non-trivial changes — it is the source of truth for scope and design.

## Tooling

- Python 3.12, managed with **uv**. `pyproject.toml` declares deps; `uv.lock` is committed.
- Database: Postgres on Fly.io.
- LLM: Anthropic API (`claude-opus-4-7`) with the `web_search_20250305` tool. Use prompt caching on the static `news.pr` block.
- PDF: WeasyPrint (system serif fonts, US Letter, must fit one page).
- Web: FastAPI + Jinja2 templates. Sessions via signed httponly cookie (`itsdangerous`).
- Hosting: Fly.io app + scheduled machines (cron at `0 0 * * *` and `0 12 * * *`, TZ `America/New_York`).

## Common commands

```bash
uv sync                                  # install / update from uv.lock
uv run uvicorn app.main:app --reload     # run web server
uv run python -m app.generate noon       # one generation cycle (slot: midnight|noon|refresh)
uv run alembic upgrade head              # apply migrations
uv run alembic revision --autogenerate -m "msg"
uv run pytest                            # all tests
uv run pytest tests/test_auth.py::test_admin_only -x   # single test
uv run ruff check                        # lint
uv run ruff format                       # format
fly deploy                               # deploy app + scheduled machines
```

## Architecture (big picture)

Two entry points share the same generation pipeline:

1. **Cron** (`0 0` and `0 12`) → Fly scheduled machine runs `python -m app.generate <slot>`.
2. **POST /refresh** (any authorized viewer) → server runs the same `app.generate` flow inline.

Pipeline (`app/generate.py`):
1. Read `news.pr` from the repo (single source of truth for the prompt).
2. Overlay DB state: `hidden_movies`, `hidden_calendars`, `important_events`.
3. Live-list Google calendars (so newly subscribed ones appear automatically) and fetch events for `[today, today+30d]` from non-hidden calendars; drop past events.
4. Substitute `{{...}}` placeholders into `news.pr` and call Claude with `web_search`.
5. Parse the strict-JSON response into `html` + `pdf_html`. Render `pdf_html` through WeasyPrint.
6. **Latest-wins upsert** into `editions` keyed by `date` — no `slot` column. Noon overwrites midnight; a manual refresh after noon overwrites that.

## Auth model (two tiers)

- **Viewer** — any email in `users.txt` (committed to repo). Can view any past edition and click Refresh.
- **Admin** — hardcoded `vtlinh87@gmail.com` constant in `app/auth.py`. Can also Hide movies, hide calendars, and edit important events.

Server-side enforcement via `require_viewer` and `require_admin` FastAPI dependencies. The viewer template hides admin-only UI but never relies on that for security. Adding/removing authorized users is done by editing `users.txt` and redeploying — there is intentionally no admin route for it.

## news.pr — the prompt file

`news.pr` at the repo root is the prompt template, with `{{...}}` placeholders filled at generation time. The fenced `<!-- CUSTOM_TOPICS_BEGIN --> ... <!-- CUSTOM_TOPICS_END -->` block is a freeform area the user edits to add new topic instructions; everything inside it is forwarded verbatim into the prompt. Keep the fence intact when editing.

Sources rendering is HTML-only — never include source citations in `pdf_html`.

## Things to be careful about

- Movie dedup: early-access vs wide-release entries for the same film. Show early-access while its date is in the future; switch to wide-release once past. Skip re-releases of films originally released > 1 year ago, and any title in `hidden_movies` whose `hidden_until >= today`.
- Stocks "why it moved" blurb appears **only** when `|daily %| > 5`.
- Weather is for **15 Hunter Ridge, Woodcliff Lake, NJ 07677**; coordinates are passed via `{{WEATHER_COORDS}}`.
- Calendar fetch always drops past events and must include calendars added to Google after deploy (re-list `calendarList` each run; do not cache the calendar list across runs).
- Mobile-first responsive layout (375px / 768px / 1200px breakpoints, `clamp()` typography, ≥44px touch targets).

## Repo layout

`app/` is the FastAPI app + generation pipeline. `tests/` mirrors module names. `migrations/` is Alembic. `scripts/google_oauth_setup.py` is a one-time local helper to mint the calendar refresh token. `fly.toml` + `Dockerfile` configure deploy.
