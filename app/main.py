from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session, defer

from app import auth, cache, calendar_oauth, calendar_summary, overlays, prefs, weather
from app import movies as movies_mod
from app.calendar_oauth import list_calendars
from app.db import Edition, HiddenCalendar, ImportantEvent, SubsectionImage, get_session
from app.settings import REPO_ROOT, get_settings, local_today


@asynccontextmanager
async def _lifespan(app):
    # NOTE: do NOT clear the refresh lock on startup — the refresh now runs
    # in a subprocess detached from this process, so it survives uvicorn
    # --reload, crashes, and graceful restarts. The 12-min stale timeout in
    # the cache layer handles truly-dead workers.
    yield


app = FastAPI(title="Linh News", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(REPO_ROOT / "app" / "templates"))
# Serve the masthead font (and any future static assets) at /fonts/*. Used by
# base.html's @font-face rule so the home-page masthead matches the PDF's.
app.mount(
    "/fonts",
    StaticFiles(directory=str(REPO_ROOT / "app" / "fonts")),
    name="fonts",
)


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


def _inject_weather(html: str, s: Session, edition: Edition | None) -> str:
    """Replace ``<!-- WEATHER_PLACEHOLDER -->`` with the hand-written prose
    paragraph baked at generation time on
    ``Edition.weather_forecast_json["prose_html"]``.

    Older editions generated before the prose pipeline (no ``prose_html``
    key) gracefully fall back to the legacy one-line live strip.
    """
    if "<!-- WEATHER_PLACEHOLDER -->" not in html:
        return html
    forecast = (edition.weather_forecast_json if edition else None) or {}
    alerts = (edition.weather_alerts_json if edition else None) or []
    refreshed_at = edition.generated_at if edition else None
    prose_html = forecast.get("prose_html") or ""
    if prose_html:
        refreshed_html = weather.build_refreshed_span(refreshed_at)
        replacement = f'<div class="weather">{refreshed_html}{prose_html}</div>'
    else:
        coords = get_settings().weather_coords
        now = weather.get_now_cached(s, coords)
        replacement = weather.build_weather_strip(
            now, forecast, alerts, refreshed_at=refreshed_at
        )
    return html.replace("<!-- WEATHER_PLACEHOLDER -->", replacement, 1)


def _inject_calendar(html: str, s: Session, today: date) -> str:
    """Replace <!-- CALENDAR_PLACEHOLDER --> with live calendar from DB."""
    if "<!-- CALENDAR_PLACEHOLDER -->" not in html:
        return html
    section = calendar_summary.load_calendar_section(s, today)
    return html.replace("<!-- CALENDAR_PLACEHOLDER -->", section, 1)


def _inject_movies(html: str, s: Session, today: date) -> str:
    """Replace ``<!-- MOVIES_PLACEHOLDER -->`` with the cached movie list,
    filtered to the daily-edition date window and the admin-hidden titles.

    Server-rendered at view-time (not generation-time) so the admin's
    hide / unhide toggles take effect on the next page reload without
    waiting for the next refresh cycle.

    Reads the cache directly — never triggers a fetch. The generation
    pipeline owns refreshing the movie cache; a viewer request must not
    block on a 30–60 s LLM call when the cache is stale or empty."""
    if "<!-- MOVIES_PLACEHOLDER -->" not in html:
        return html
    cached = movies_mod.get_movies()
    if not cached:
        return html.replace("<!-- MOVIES_PLACEHOLDER -->", "", 1)
    hidden = {m["title"] for m in overlays.all_hidden_movies(s)}
    favorites = overlays.favorite_movie_titles(s)
    allowed = set(prefs.get_allowed_ratings(today))
    section = movies_mod.render_html_section(
        cached,
        today,
        hidden_titles=hidden,
        allowed_ratings=allowed,
        favorite_titles=favorites,
    )
    return html.replace("<!-- MOVIES_PLACEHOLDER -->", section, 1)


def _render_viewer(request: Request, day: date, s: Session, viewer_email: str) -> HTMLResponse:
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
        edition_html = _inject_weather(edition_html, s, edition)
        # Calendar and movies honor the viewed date (date picker), not now —
        # so picking a past edition shows that edition's calendar/movies.
        edition_html = _inject_calendar(edition_html, s, day)
        edition_html = _inject_movies(edition_html, s, day)
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
            "is_admin": auth.is_admin(viewer_email),
            # First-paint hint so the button renders in the right state with
            # no flash if a background refresh is already running.
            "refresh_in_progress": cache.edition_refresh_in_progress(),
            "edition_generated_at": (edition.generated_at.isoformat() if edition else None),
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


def _pdf_filename(day: date | str, pdf_bytes: bytes) -> str:
    """Build the user-facing PDF download name with a 10-char content-hash
    suffix. Each newly generated edition gets a distinct filename even when
    a same-day refresh overwrites the previous version, which makes saved
    copies easy to tell apart and defeats stale browser caches.
    """
    digest = hashlib.sha256(pdf_bytes).hexdigest()[:10]
    return f"linh-times-{day}-{digest}.pdf"


def _pdf_token_valid(request: Request, token: str | None) -> bool:
    """True iff the request presents a valid PDF_LATEST_TOKEN, either as
    ``?token=…`` or as ``Authorization: Bearer …``. An unset/empty
    expected token always fails (token auth disabled)."""
    expected = get_settings().pdf_latest_token
    auth_header = request.headers.get("authorization", "")
    bearer = (
        auth_header[len("Bearer ") :].strip() if auth_header.lower().startswith("bearer ") else ""
    )
    presented = (token or bearer or "").strip()
    if not expected or not presented:
        return False
    return secrets.compare_digest(presented, expected)


@app.get("/edition-image/{image_id}")
def edition_image(
    image_id: int,
    s: Session = Depends(get_session),
):
    """Stream the bytes of one ``subsection_images`` row.

    No auth gate — these are server-resized assets keyed by an opaque integer
    that only appears in viewer pages a logged-in user already has. Cached
    aggressively because each id is content-addressed by generation.
    """
    row = s.get(SubsectionImage, image_id)
    if not row:
        raise HTTPException(404, "image not found")
    return Response(
        row.bytes_,
        media_type=row.mime_type or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/pdf/latest")
def pdf_latest(
    request: Request,
    token: str | None = None,
    s: Session = Depends(get_session),
):
    """Return the most recently generated PDF. Public, gated by a shared
    secret — no Google login required. Accepts the secret either as
    ``?token=…`` (handy for bookmarks / home-screen shortcuts) or as
    ``Authorization: Bearer …`` (preferred — query strings end up in
    proxy and Fly access logs)."""
    if not _pdf_token_valid(request, token):
        raise HTTPException(401, "Invalid or missing token")
    edition = s.execute(
        select(Edition)
        .order_by(Edition.date.desc())
        .options(defer(Edition.html), defer(Edition.pdf_html))
        .limit(1)
    ).scalar_one_or_none()
    if not edition:
        raise HTTPException(404, "No editions available yet")
    filename = _pdf_filename(edition.date, edition.pdf)
    return Response(
        edition.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/pdf/{day}")
def view_pdf(
    day: str,
    request: Request,
    token: str | None = None,
    s: Session = Depends(get_session),
):
    if not _pdf_token_valid(request, token):
        auth.require_viewer(request, s)
    edition = s.execute(
        select(Edition)
        .where(Edition.date == _parse_date(day))
        .options(defer(Edition.html), defer(Edition.pdf_html))
    ).scalar_one_or_none()
    if not edition:
        raise HTTPException(404, "No edition for that date")
    filename = _pdf_filename(day, edition.pdf)
    return Response(
        edition.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
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
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
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
    # Only need generated_at — skip the multi-MB pdf / pdf_html / html columns.
    row = s.execute(
        select(Edition.generated_at).where(Edition.date == _parse_date(day))
    ).scalar_one_or_none()
    return {
        "generated_at": row.isoformat() if row else None,
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
        cache.maybe_refresh_in_background(lambda: _refresh_events_cache_via(Maker))
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
        s,
        active_ids,
        today,
        horizon,
        calendar_names=cal_name,
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


_ALL_MOVIE_RATINGS = ["G", "PG", "PG-13", "R", "NC-17", "Unrated"]
# "Unrated" is a synthetic checkbox covering rows whose rating field is
# empty (TMDB hasn't filled in the MPAA yet — typical for announced
# pre-release sequels like Angry Birds 3) or explicitly "NR". Off by
# default in the kid-friendly view; toggle it on to surface those rows.
_UNRATED_CERTS = {"", "NR"}


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
            "default_ratings": prefs.get_allowed_ratings(),
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
    selected = set(requested) if requested else set(prefs.get_allowed_ratings())
    include_unrated = "Unrated" in selected
    movies = movies_mod.get_movies(refresh_if_stale=bool(refresh))
    favorites = overlays.favorite_movie_titles(s)

    def _matches(m: dict) -> bool:
        # Favorites bypass the rating filter — always visible regardless of
        # selected MPAA boxes.
        if m.get("title") in favorites:
            return True
        rating = (m.get("rating") or "").strip()
        if rating in selected:
            return True
        return include_unrated and rating in _UNRATED_CERTS

    movies = [m for m in movies if _matches(m)]
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
    hidden = {m["title"] for m in overlays.all_hidden_movies(s)}
    return {
        "movies": [
            {
                **m,
                "hidden": m["title"] in hidden,
                "favorite": m["title"] in favorites,
            }
            for m in movies
        ],
        "cache_age_seconds": movies_mod.movies_cache_age_seconds(),
    }


@app.post("/admin/movies/ratings")
async def admin_movies_ratings(
    request: Request,
    email: str = Depends(auth.require_admin),
):
    """Persist the admin's MPAA-rating selection. Used by both the daily
    HTML edition's Movies section and the printed Linh Times PDF."""
    body = await request.json()
    raw = body.get("ratings") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "ratings must be a list")
    saved = prefs.set_allowed_ratings([str(r) for r in raw])
    return {"ratings": saved}


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


@app.post("/admin/movies/favorite")
async def admin_movies_favorite(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    """Toggle a movie's favorite flag. Favorites bypass the MPAA-rating
    filter on the admin page and force inclusion in the daily edition / PDF
    when their release falls in the favorite window."""
    body = await request.json()
    title = (body.get("title") or "").strip()
    favorite = bool(body.get("favorite"))
    if not title:
        raise HTTPException(400, "title required")
    if favorite:
        overlays.favorite_movie(s, title)
    else:
        overlays.unfavorite_movie(s, title)
    return {"ok": True, "favorite": favorite}


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
