from __future__ import annotations

import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, defer

from app import auth, cache, calendar_oauth, calendar_summary, overlays, tmdb
from app import movies as movies_mod
from app.calendar_oauth import list_calendars
from app.db import Edition, HiddenCalendar, ImportantEvent, get_session
from app.settings import ADMIN_EMAIL, REPO_ROOT, get_settings, local_today


@asynccontextmanager
async def _lifespan(app):
    # NOTE: do NOT clear the refresh lock on startup — the refresh now runs
    # in a subprocess detached from this process, so it survives uvicorn
    # --reload, crashes, and graceful restarts. The 12-min stale timeout in
    # the cache layer handles truly-dead workers.
    yield


app = FastAPI(title="Linh News", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(REPO_ROOT / "app" / "templates"))


@app.middleware("http")
async def _canonical_host(request: Request, call_next):
    """Redirect 127.0.0.1 → localhost so OAuth state cookies stay scoped to
    a single hostname. Without this, hitting 127.0.0.1:8000 sets cookies on
    one origin while the OAuth callback (which uses PUBLIC_BASE_URL =
    localhost) lands on another, causing 'invalid_state'."""
    host = request.headers.get("host", "")
    if host.startswith("127.0.0.1"):
        new_host = host.replace("127.0.0.1", "localhost", 1)
        new_url = request.url.replace(netloc=new_host)
        return RedirectResponse(str(new_url), status_code=307)
    return await call_next(request)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ───────────────────────── Auth ─────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    state = secrets.token_urlsafe(16)
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {"login_url": auth.login_url(state), "error": request.query_params.get("error")},
    )
    resp.set_cookie("oauth_state", state, httponly=True, samesite="lax", max_age=600)
    return resp


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    s: Session = Depends(get_session),
):
    if not code or state != request.cookies.get("oauth_state"):
        return RedirectResponse("/login?error=invalid_state")
    try:
        email = await auth.exchange_code_for_email(code)
    except HTTPException:
        return RedirectResponse("/login?error=oauth_failed")
    if not auth.is_allowed(email):
        return RedirectResponse("/login?error=not_authorized")
    sid = auth.create_session(s, email)
    resp = RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        sid,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=get_settings().session_ttl_days * 86400,
    )
    resp.delete_cookie("oauth_state")
    return resp


@app.get("/logout")
def logout(request: Request, s: Session = Depends(get_session)):
    sid = request.cookies.get(auth.SESSION_COOKIE)
    if sid:
        auth.delete_session(s, sid)
    resp = RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ──────────────────────── Viewer ────────────────────────

def _inject_calendar(html: str, s: Session, today: date) -> str:
    """Replace <!-- CALENDAR_PLACEHOLDER --> with live calendar from DB."""
    if "<!-- CALENDAR_PLACEHOLDER -->" not in html:
        return html
    section = calendar_summary.load_calendar_section(s, today)
    return html.replace("<!-- CALENDAR_PLACEHOLDER -->", section, 1)


def _render_viewer(
    request: Request, day: date, s: Session, viewer_email: str
) -> HTMLResponse:
    # Defer the multi-MB pdf column — the home page only needs html +
    # generated_at. Fetching pdf on every request through the Fly proxy
    # was the dominant page-load cost.
    edition = s.execute(
        select(Edition)
        .where(Edition.date == day)
        .options(defer(Edition.pdf), defer(Edition.pdf_html))
    ).scalar_one_or_none()
    today = local_today()
    next_date = day + timedelta(days=1)
    edition_html = edition.html if edition else None
    if edition_html:
        edition_html = _inject_calendar(edition_html, s, today)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "edition_date": day.isoformat(),
            "prev_date": (day - timedelta(days=1)).isoformat(),
            "next_date": next_date.isoformat(),
            "next_date_allowed": next_date <= today,
            "today_str": today.isoformat(),
            "edition_html": edition_html,
            "is_admin": viewer_email.lower() == ADMIN_EMAIL.lower(),
            # First-paint hint so the button renders in the right state with
            # no flash if a background refresh is already running.
            "refresh_in_progress": cache.edition_refresh_in_progress(),
            "edition_generated_at": (
                edition.generated_at.isoformat() if edition else None
            ),
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    email: str = Depends(auth.require_viewer),
    s: Session = Depends(get_session),
):
    today = local_today()
    return _render_viewer(request, today, s, email)


@app.get("/d/{day}", response_class=HTMLResponse)
def view_date(
    day: str,
    request: Request,
    email: str = Depends(auth.require_viewer),
    s: Session = Depends(get_session),
):
    return _render_viewer(request, _parse_date(day), s, email)


@app.get("/pdf/latest")
def pdf_latest(
    token: str | None = None,
    s: Session = Depends(get_session),
):
    """Return the most recently generated PDF. Authenticated via ?token=
    (no login required) so it can be bookmarked or used as a home-screen shortcut."""
    expected = get_settings().pdf_latest_token
    if not expected or not token or token != expected:
        raise HTTPException(401, "Invalid or missing token")
    edition = s.execute(
        select(Edition).order_by(Edition.date.desc()).limit(1)
    ).scalar_one_or_none()
    if not edition:
        raise HTTPException(404, "No editions available yet")
    filename = f"linh-times-{edition.date}.pdf"
    return Response(
        edition.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.get("/pdf/{day}")
def view_pdf(
    day: str,
    s: Session = Depends(get_session),
    email: str = Depends(auth.require_viewer),
):
    edition = s.get(Edition, _parse_date(day))
    if not edition:
        raise HTTPException(404, "No edition for that date")
    return Response(
        edition.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="linh-times-{day}.pdf"'},
    )


def _spawn_generate_subprocess(slot: str, target_date: str | None = None) -> int:
    """Launch `python -m app.generate refresh [--date YYYY-MM-DD]` as a
    detached subprocess.

    Detached means: when the FastAPI process is killed (uvicorn --reload, a
    crash, or graceful shutdown), the worker keeps running. The worker holds
    its own lifecycle: it clears ``cache.end_edition_refresh()`` in its
    `finally` block when done. If the worker itself dies, the cache's 12-min
    stale timeout kicks in.
    """
    cmd = [sys.executable, "-m", "app.generate", slot]
    if target_date:
        cmd += ["--date", target_date]
    env = os.environ.copy()
    # Make sure the worker inherits the same .env / repo root.
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Inherit the parent's stdout/stderr (None) so refresh logs appear live in
    # the dev terminal that's running uvicorn. The subprocess also writes to a
    # per-run log file inside _cli() (configured in app/generate.py) for
    # durability when the parent terminal is closed.
    kwargs: dict = {
        "cwd": str(REPO_ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": None,
    }
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS make the child outlive
        # the parent (and not receive Ctrl-C signals sent to uvicorn).
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        # New session + setsid so SIGINT/SIGTERM to the parent doesn't
        # propagate to the child.
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


@app.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(request: Request, email: str = Depends(auth.require_admin)):
    """Kick a background regeneration in a *detached subprocess* so the
    refresh keeps running even if uvicorn restarts. Returns 202 immediately
    so the page can keep showing the old edition until the new one is ready.

    Accepts an optional JSON body ``{"date": "YYYY-MM-DD"}`` to regenerate a
    specific date instead of today."""
    target_date: str | None = None
    try:
        body = await request.json()
        raw = (body.get("date") or "").strip()
        if raw:
            date.fromisoformat(raw)  # validate
            target_date = raw
    except Exception:  # noqa: BLE001 — missing/invalid body is fine
        pass
    effective_date = target_date or local_today().isoformat()
    if not cache.begin_edition_refresh():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A refresh is already in progress.",
        )
    pid = _spawn_generate_subprocess("refresh", target_date=target_date)
    cache.set_edition_refresh_pid(pid)
    return {"ok": True, "date": effective_date, "in_progress": True}


@app.post("/cron/{slot}", status_code=status.HTTP_202_ACCEPTED)
def cron_trigger(slot: str, request: Request):
    """Cron-pinged endpoint. GitHub Actions hits this twice a day with the
    shared CRON_SECRET in the X-Cron-Token header. Spawns the same detached
    subprocess that /refresh uses, so this returns immediately."""
    if slot not in {"morning", "evening"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown slot")
    expected = get_settings().cron_secret
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CRON_SECRET not configured")
    sent = request.headers.get("x-cron-token", "")
    if not secrets.compare_digest(sent, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad cron token")
    pid = _spawn_generate_subprocess(slot)
    return {"ok": True, "slot": slot, "pid": pid}


@app.get("/editions/{day}/freshness")
def edition_freshness(
    day: str,
    s: Session = Depends(get_session),
    email: str = Depends(auth.require_viewer),
):
    edition = s.get(Edition, _parse_date(day))
    return {
        "generated_at": edition.generated_at.isoformat() if edition else None,
        "refresh_in_progress": cache.edition_refresh_in_progress(),
        "last_error": cache.get_recent_edition_refresh_error(),
        "expected_seconds": cache.expected_refresh_seconds(),
    }


# ──────────────────────── Admin ─────────────────────────

@app.post("/hide-movie")
async def hide_movie(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    overlays.hide_movie(s, title)
    return {"ok": True}


def _calendar_events_for_year_cached(s: Session, _email: str) -> tuple[list[dict], list[dict]]:
    """Return cached events immediately. Kicks a background refresh if the
    cache is older than ``events_refresh_min_seconds``. On a cold start
    (cache empty OR previous fetch returned zero events) we synchronously
    fetch so the page never renders an empty list when calendars exist."""
    events, _ = cache.get_events()
    if not events:  # None OR empty list — both indicate "no usable cache"
        events, _ = _calendar_events_for_year(s)
        cache.store_events(events)
    else:
        Maker = _session_factory_for_background()
        cache.maybe_refresh_in_background(
            lambda: _refresh_events_cache_via(Maker)
        )
    return events, []


def _session_factory_for_background():
    from app.db import session_factory
    return session_factory()


def _refresh_events_cache_via(maker) -> list[dict]:
    with maker() as bs:
        events, _ = _calendar_events_for_year(bs)
    return events


def _calendar_events_for_year(s: Session) -> tuple[list[dict], list[dict]]:
    """Return (deduped_upcoming_events, all_calendars).
    Each event appears once: the nearest upcoming occurrence per iCalUID.
    Hidden calendars are skipped entirely (no API queries to them)."""
    today = local_today()
    horizon = today + timedelta(days=365)
    all_cals = list_calendars(s)
    cal_name = {c["id"]: c["name"] for c in all_cals}
    hidden_ids = {c["id"] for c in overlays.hidden_calendar_ids(s)}
    active_ids = [c["id"] for c in all_cals if c["id"] not in hidden_ids]
    raw = calendar_oauth.fetch_events(
        s, active_ids, today, horizon, calendar_names=cal_name,
    )
    nearest: dict[str, dict] = {}
    seen_starts: dict[str, set] = {}
    for ev in raw:
        uid = ev.get("ical_uid")
        if not uid:
            continue
        title = (ev.get("summary") or "").strip()
        if not title:
            continue
        date_str = (ev.get("start") or "")[:10]
        if uid not in nearest:
            cn = cal_name.get(ev.get("calendar_id"), ev.get("calendar_id"))
            nearest[uid] = {
                "ical_uid": uid,
                "calendar_id": ev.get("calendar_id"),
                "title": title,
                "date": date_str,
                "calendar_name": cn,
                "recurring": False,
                "auto_important": calendar_oauth.is_auto_important(cn, title),
            }
            seen_starts[uid] = {date_str}
        else:
            seen_starts[uid].add(date_str)
            if len(seen_starts[uid]) > 1:
                nearest[uid]["recurring"] = True
            # Latest title/date wins (for events whose details were edited).
            if date_str < (nearest[uid]["date"] or "9999"):
                nearest[uid]["date"] = date_str
            nearest[uid]["title"] = title
    out = sorted(nearest.values(), key=lambda e: e["date"])
    return out, all_cals


def _invalidate_events_cache() -> None:
    """Force the next admin/events fetch to refresh the cache from Google."""
    cache.store_events([])  # placeholder so freshness pulse changes
    # We deliberately don't write 0 — clients reload on updated_at change.


@app.get("/admin/events")
def admin_events_get_redirect(email: str = Depends(auth.require_admin)):
    """Events have been merged into the Calendars admin page."""
    return RedirectResponse("/admin/calendars", status_code=302)


@app.get("/admin/events/freshness")
def admin_events_freshness(
    email: str = Depends(auth.require_admin),
):
    _, updated_at = cache.get_events()
    return {"updated_at": updated_at}


@app.get("/admin/events/data")
def admin_events_data(
    offset: int = 0,
    limit: int = 50,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    cal_events, _ = _calendar_events_for_year_cached(s, email)
    # Filter out events from currently-hidden calendars at READ time so toggles
    # take effect immediately without forcing a Google re-fetch.
    hidden_ids = {c["id"] for c in overlays.hidden_calendar_ids(s)}
    cal_events = [e for e in cal_events if e.get("calendar_id") not in hidden_ids]
    page = cal_events[offset : offset + limit]
    rows = s.execute(select(ImportantEvent.ical_uid)).all()
    important_uids = {r[0] for r in rows if r[0]}
    suppressed_uids = overlays.suppressed_event_uids(s)
    return {
        "events": [
            {
                **e,
                "important": e.get("auto_important") or e["ical_uid"] in important_uids,
                "locked": bool(e.get("auto_important")),
                "suppressed": e["ical_uid"] in suppressed_uids,
            }
            for e in page
        ],
        "has_more": offset + limit < len(cal_events),
        "total": len(cal_events),
    }


@app.post("/admin/events/suppress")
async def admin_events_suppress(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    """Mark or unmark a calendar event (by iCalUID) as suppressed.
    Suppressed events are excluded entirely from HTML and PDF generation."""
    body = await request.json()
    ical_uid = (body.get("ical_uid") or "").strip()
    suppressed = bool(body.get("suppressed"))
    if not ical_uid:
        raise HTTPException(400, "ical_uid required")
    if suppressed:
        cal_events, _ = _calendar_events_for_year_cached(s, email)
        match = next((e for e in cal_events if e["ical_uid"] == ical_uid), None)
        title = match["title"] if match else ical_uid
        overlays.suppress_event(s, ical_uid, title)
        # Mutually exclusive: a suppressed event can't be important.
        s.execute(delete(ImportantEvent).where(and_(ImportantEvent.ical_uid == ical_uid)))
        s.commit()
    else:
        overlays.unsuppress_event(s, ical_uid)
    return {"ok": True, "suppressed": suppressed}


@app.post("/admin/events/toggle")
async def admin_events_toggle(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    """Mark or unmark a single calendar event (by iCalUID) as important.
    Marking by UID covers all recurring instances and survives edits to the
    event's title, date, location, etc."""
    body = await request.json()
    ical_uid = (body.get("ical_uid") or "").strip()
    important = bool(body.get("important"))
    if not ical_uid:
        raise HTTPException(400, "ical_uid required")
    s.execute(delete(ImportantEvent).where(and_(ImportantEvent.ical_uid == ical_uid)))
    if important:
        cal_events, _ = _calendar_events_for_year_cached(s, email)
        match = next((e for e in cal_events if e["ical_uid"] == ical_uid), None)
        if not match or not match.get("date"):
            raise HTTPException(404, "event not found in calendar")
        s.add(
            ImportantEvent(
                ical_uid=ical_uid,
                title=match["title"],
                event_date=_parse_date(match["date"]),
                importance=8,
                notes="from calendar",
            )
        )
    s.commit()
    return {"ok": True, "important": important}


@app.post("/admin/events/{event_id}/delete")
def admin_events_delete(
    event_id: int,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    s.execute(delete(ImportantEvent).where(ImportantEvent.id == event_id))
    s.commit()
    return RedirectResponse("/admin/events", status_code=303)


@app.get("/admin/calendars", response_class=HTMLResponse)
def admin_calendars_get(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    try:
        calendars = list_calendars(s)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    hidden_ids = {c["id"] for c in overlays.hidden_calendar_ids(s)}
    # Sort: visible calendars first (primary first within that), hidden last.
    calendars.sort(key=lambda c: (c["id"] in hidden_ids, not c.get("primary"), c["name"].lower()))
    return templates.TemplateResponse(
        request,
        "admin_calendars.html",
        {"calendars": calendars, "hidden_ids": hidden_ids},
    )


_ALL_MOVIE_RATINGS = ["G", "PG", "PG-13", "R", "NC-17"]


@app.get("/admin/movies", response_class=HTMLResponse)
def admin_movies_get(
    request: Request,
    email: str = Depends(auth.require_admin),
):
    return templates.TemplateResponse(
        request,
        "admin_movies.html",
        {
            "all_ratings": _ALL_MOVIE_RATINGS,
            "default_ratings": calendar_oauth.allowed_movie_ratings(),
        },
    )


@app.get("/admin/movies/data")
def admin_movies_data(
    request: Request,
    refresh: int = 0,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    requested = [r for r in request.query_params.getlist("ratings") if r in _ALL_MOVIE_RATINGS]
    selected = set(requested) if requested else set(calendar_oauth.allowed_movie_ratings())
    movies = movies_mod.get_or_fetch_movies(force=bool(refresh))
    movies = [m for m in movies if m.get("rating") in selected]
    # Drop "Currently in theaters" entries that opened more than 3 weeks ago —
    # those are no longer relevant suggestions.
    today = local_today()
    cutoff = today - timedelta(weeks=3)
    def _still_fresh(m: dict) -> bool:
        if m.get("status") != "in_theaters":
            return True
        try:
            return _parse_date(m.get("release_date", "")) >= cutoff
        except Exception:  # noqa: BLE001
            return True
    movies = [m for m in movies if _still_fresh(m)]
    # Backfill missing posters via TMDB (and replace any obviously bad ones).
    for m in movies:
        url = m.get("poster_url") or ""
        if not url or "search" in url or not url.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            year = None
            try:
                year = int((m.get("release_date") or "")[:4])
            except (TypeError, ValueError):
                year = None
            tmdb_url = tmdb.lookup_poster(m["title"], year=year)
            if tmdb_url:
                m["poster_url"] = tmdb_url
    hidden = {m["title"] for m in overlays.all_hidden_movies(s)}
    return {
        "movies": [{**m, "hidden": m["title"] in hidden} for m in movies],
        "cache_age_seconds": cache.movies_cache_age(),
    }


@app.post("/admin/movies/toggle")
async def admin_movies_toggle(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    body = await request.json()
    title = (body.get("title") or "").strip()
    hide = bool(body.get("hidden"))
    if not title:
        raise HTTPException(400, "title required")
    if hide:
        overlays.hide_movie(s, title)
    else:
        overlays.unhide_movie(s, title)
    return {"ok": True, "hidden": hide}


@app.post("/admin/movies/poster")
async def admin_movies_poster(
    request: Request,
    email: str = Depends(auth.require_admin),
):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    year: int | None = None
    try:
        year = int(body["year"]) if body.get("year") else None
    except (TypeError, ValueError):
        year = None

    poster_url = tmdb.lookup_poster(title, year=year)
    if poster_url:
        movies, _ = cache.get_movies()
        if movies:
            updated = False
            for m in movies:
                if m.get("title") == title and not m.get("poster_url"):
                    m["poster_url"] = poster_url
                    updated = True
                    break
            if updated:
                cache.store_movies(movies)
    return {"poster_url": poster_url}


@app.get("/admin/stocks", response_class=HTMLResponse)
def admin_stocks_get(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "admin_stocks.html",
        {"symbols": overlays.watchlist_symbols(s)},
    )


@app.post("/admin/stocks/add")
async def admin_stocks_add(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    body = await request.json()
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol or not symbol.isalnum() or len(symbol) > 8:
        raise HTTPException(400, "Invalid symbol — letters/digits, up to 8 chars.")
    overlays.add_watchlist_symbol(s, symbol)
    return {"ok": True, "symbol": symbol}


@app.post("/admin/stocks/remove")
async def admin_stocks_remove(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    body = await request.json()
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol required")
    overlays.remove_watchlist_symbol(s, symbol)
    return {"ok": True}


@app.post("/admin/calendars/toggle")
async def admin_calendars_toggle(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    """Hide or unhide a single calendar by id."""
    body = await request.json()
    calendar_id = (body.get("calendar_id") or "").strip()
    hidden = bool(body.get("hidden"))
    if not calendar_id:
        raise HTTPException(400, "calendar_id required")
    try:
        all_cals = {c["id"]: c["name"] for c in list_calendars(s)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    if calendar_id not in all_cals:
        raise HTTPException(404, "calendar not found")
    if hidden:
        # Hide is fast: events from this calendar are filtered out at read
        # time. No need to dump the cache.
        if not s.get(HiddenCalendar, calendar_id):
            s.add(HiddenCalendar(calendar_id=calendar_id, calendar_name=all_cals[calendar_id]))
        s.commit()
    else:
        # Un-hide must re-query Google for events from the now-visible
        # calendar — invalidate the cache so the next read triggers a fetch.
        s.execute(delete(HiddenCalendar).where(HiddenCalendar.calendar_id == calendar_id))
        s.commit()
        _invalidate_events_cache()
    return {"ok": True, "hidden": hidden}


# ──────────────────────── Helpers ───────────────────────

def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(400, f"Invalid date: {s}") from e
