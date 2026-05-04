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
- LLM: Anthropic API with the `web_search_20250305` tool. Model name comes from `settings.anthropic_model`. Use prompt caching on the static `news.pr` block. The LLM returns a structured `LinhNews` object (see `app/llm_schema.py`); the server renders both the HTML page (`app/html_renderer.py`) and the PDF (`app/pdf_renderer.py`) from that data — the LLM never produces HTML.
- PDF: WeasyPrint (system serif fonts, 15.296in × 27.193in broadsheet page, must fit one page).
- Web: FastAPI + Jinja2 templates. Sessions via signed httponly cookie (`itsdangerous`).
- Hosting: Fly.io app. Cron via GitHub Actions (`.github/workflows/cron.yml`) once daily at 11:00 UTC (6 AM EST / 7 AM EDT).

## Common commands

**Before starting the local server**, ensure the Fly Postgres proxy is running. Check and start it if needed:
```bash
# Check if proxy is already listening on 15432
netstat -an | grep 15432 || fly proxy 15432:5432 -a linh-news-db &
```
The proxy tunnels the Fly DB to `localhost:15432`. Without it the app fails to connect on startup.

```bash
uv sync                                  # install / update from uv.lock
uv run uvicorn app.main:app --reload     # run web server (requires proxy above)
uv run python -m app.generate morning    # one generation cycle (slot: morning|evening|refresh)
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

1. **Cron** (`0 7` and `0 19` in `America/New_York`) → Fly scheduled machine runs `python -m app.generate <slot>`.
2. **POST /refresh** (any authorized viewer) → server runs the same `app.generate` flow inline.

Pipeline (`app/generate.py`):
1. Read `news.pr` from the repo (single source of truth for the prompt).
2. Overlay DB state: `hidden_movies`, `hidden_calendars`, `important_events`.
3. Live-list Google calendars (so newly subscribed ones appear automatically) and fetch events for `[today, today+30d]` from non-hidden calendars; drop past events.
4. Substitute `{{...}}` placeholders into `news.pr` and call Claude with `web_search`. The response is a structured `LinhNews` object (see `app/llm_schema.py`).
5. Detect any of the 8 required sections the model omitted and re-roll each in a focused single-section call.
6. For each subsection that supplied candidate image URLs, download one (landscape preferred), resize to ≤400px wide via Pillow, persist the bytes in `subsection_images`. The viewer serves them via `GET /edition-image/{id}`.
7. Render the HTML body via `app/html_renderer.py` and the PDF input via `app/pdf_renderer.py`; pipe the PDF input through WeasyPrint.
8. **Latest-wins upsert** into `editions` keyed by `date` — no `slot` column. The 7 PM run overwrites the 7 AM run; a manual refresh after that overwrites that. The structured response is also persisted on `editions.content_json` so a re-render doesn't require another LLM call.

## Auth model (two tiers)

- **Viewer** — any email in `users.txt` (committed to repo). Can view any past edition and click Refresh.
- **Admin** — hardcoded `vtlinh87@gmail.com` constant in `app/auth.py`. Can also Hide movies, hide calendars, and edit important events.

Server-side enforcement via `require_viewer` and `require_admin` FastAPI dependencies. The viewer template hides admin-only UI but never relies on that for security. Adding/removing authorized users is done by editing `users.txt` and redeploying — there is intentionally no admin route for it.

## news.pr — the prompt file

`news.pr` at the repo root is the prompt template, with `{{...}}` placeholders filled at generation time. The fenced `<!-- CUSTOM_TOPICS_BEGIN --> ... <!-- CUSTOM_TOPICS_END -->` block is a freeform area the user edits to add new topic instructions; everything inside it is forwarded verbatim into the prompt. Keep the fence intact when editing.

Sources rendering is HTML-only — `app/html_renderer.py` adds source links to news items; `app/pdf_renderer.py` deliberately omits them from the PDF.

## Push hygiene

Whenever you push to GitHub, also re-read [README.md](README.md) and update it if it has drifted from reality. Stale README is a real problem — admin pages, deployment commands, and feature lists all change frequently. Keep it current with each push.

## Deploy

GitHub Actions is configured to deploy to Fly on push. **Do not run `fly deploy` yourself** unless the user explicitly asks for it — pushing to GitHub is sufficient.

## Verifying generated content

Whenever you trigger a new edition (e.g. `python -m app.generate refresh`, calling `/refresh`, or making a change that affects the prompt or the data assembled into it), **always verify the result before declaring success**:

1. **HTML loaded into the DB**: query the `editions` row for that date and confirm `html` is non-empty and `pdf` starts with `%PDF`.
2. **Sections present and sensible**:
   - Each requested section (politics, NJ/NY, Dorchester, finance, AI, stocks, movies, weather, calendar) appears only if it has fresh, dated content.
   - News items end with a `Sources:` line; tooltips hold the URL.
   - Stocks: every ticker carries a "why it moved" tooltip with ≥5 sources sorted most-trusted first; the depth of the explanation scales with the size of the move.
   - Movies: hidden titles are absent; early-access vs. wide-release dedupe is correct; old re-releases (>1 year old) skipped.
   - Weather is for Woodcliff Lake 07677.
   - Calendar: no past events, hidden calendars excluded, important all-day events surface with appropriate lead time.
3. **PDF**: open it (or call `app.pdf.page_count`) and confirm one broadsheet page (15.296in × 27.193in), NYT-style masthead, no source citations, no Hide buttons.
4. **Sanity-check the *content*, not just the structure**: spot-check a couple of facts. If a section has stale or invented information, treat the run as failed and re-trigger after fixing the prompt or input data — don't ship plausible-looking garbage.

If any of the above fails, fix the underlying issue (prompt, overlays, calendar fetch, etc.) and re-generate. Never report a generation as successful purely because the run exited 0.

## Things to be careful about

- Movie dedup: early-access vs wide-release entries for the same film. Show early-access while its date is in the future; switch to wide-release once past. Skip re-releases of films originally released > 1 year ago, and any title in `hidden_movies` whose `hidden_until >= today`.
- Every stock carries a "why it moved" tooltip with ≥5 sources, depth scaled to the move size; the renderer produces the tooltip from `Stock.why_it_moved` in the LinhNews response.
- Weather is for **15 Hunter Ridge, Woodcliff Lake, NJ 07677**; coordinates are read from `settings.weather_coords` and passed directly to the NWS client. The LLM has no role in weather generation.
- Calendar fetch always drops past events and must include calendars added to Google after deploy (re-list `calendarList` each run; do not cache the calendar list across runs).
- Mobile-first responsive layout (375px / 768px / 1200px breakpoints, `clamp()` typography, ≥44px touch targets).

## Repo layout

`app/` is the FastAPI app + generation pipeline. `tests/` mirrors module names. `migrations/` is Alembic. `scripts/google_oauth_setup.py` is a one-time local helper to mint the calendar refresh token. `fly.toml` + `Dockerfile` configure deploy.
