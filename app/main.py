from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth, generate, overlays
from app.calendar_oauth import list_calendars
from app.db import Edition, HiddenCalendar, ImportantEvent, get_session
from app.settings import ADMIN_EMAIL, REPO_ROOT, get_settings

app = FastAPI(title="Linh News")
templates = Jinja2Templates(directory=str(REPO_ROOT / "app" / "templates"))


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

def _render_viewer(
    request: Request, day: date, s: Session, viewer_email: str
) -> HTMLResponse:
    edition = s.get(Edition, day)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "edition_date": day.isoformat(),
            "prev_date": (day - timedelta(days=1)).isoformat(),
            "next_date": (day + timedelta(days=1)).isoformat(),
            "edition_html": edition.html if edition else None,
            "is_admin": viewer_email.lower() == ADMIN_EMAIL.lower(),
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    email: str = Depends(auth.require_viewer),
    s: Session = Depends(get_session),
):
    today = datetime.now(UTC).date()
    return _render_viewer(request, today, s, email)


@app.get("/d/{day}", response_class=HTMLResponse)
def view_date(
    day: str,
    request: Request,
    email: str = Depends(auth.require_viewer),
    s: Session = Depends(get_session),
):
    return _render_viewer(request, _parse_date(day), s, email)


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


@app.post("/refresh")
def refresh(email: str = Depends(auth.require_viewer)):
    today = datetime.now(UTC).date()
    generate.run("refresh", today=today)
    return {"ok": True, "date": today.isoformat()}


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


@app.get("/admin/events", response_class=HTMLResponse)
def admin_events_get(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    rows = s.execute(
        select(ImportantEvent).order_by(ImportantEvent.event_date)
    ).scalars().all()
    return templates.TemplateResponse(request, "admin_events.html", {"events": rows})


@app.post("/admin/events")
def admin_events_post(
    title: str = Form(...),
    event_date: str = Form(...),
    importance: int = Form(5),
    notes: str = Form(""),
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    s.add(
        ImportantEvent(
            title=title.strip(),
            event_date=_parse_date(event_date),
            importance=max(1, min(10, importance)),
            notes=notes.strip() or None,
        )
    )
    s.commit()
    return RedirectResponse("/admin/events", status_code=303)


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
    return templates.TemplateResponse(
        request,
        "admin_calendars.html",
        {"calendars": calendars, "hidden_ids": hidden_ids},
    )


@app.post("/admin/calendars")
async def admin_calendars_post(
    request: Request,
    email: str = Depends(auth.require_admin),
    s: Session = Depends(get_session),
):
    form = await request.form()
    selected = set(form.getlist("hidden"))
    try:
        all_cals = {c["id"]: c["name"] for c in list_calendars(s)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    s.execute(delete(HiddenCalendar))
    for cal_id in selected:
        if cal_id in all_cals:
            s.add(HiddenCalendar(calendar_id=cal_id, calendar_name=all_cals[cal_id]))
    s.commit()
    return RedirectResponse("/admin/calendars", status_code=303)


# ──────────────────────── Helpers ───────────────────────

def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(400, f"Invalid date: {s}") from e
