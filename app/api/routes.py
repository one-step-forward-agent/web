from pathlib import Path
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from uuid import uuid4
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.models.models import Calendar, Event, EventFile, Integration, User
from app.schemas import AssistantConfirmation, AssistantMessage, AssistantResponse, CalendarCreate, CalendarRead, EventCreate, EventRead, EventUpdate, UserCreate, UserRead

router = APIRouter(prefix="/api")
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="User with this email already exists")
    user = User(**payload.model_dump())
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/users", response_model=list[UserRead])
async def list_users(session: AsyncSession = Depends(get_session)):
    return list((await session.scalars(select(User).order_by(User.id))).all())


@router.get("/users/{user_id}", response_model=UserRead)
async def get_user(user_id: int, session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/calendars", response_model=list[CalendarRead])
async def list_calendars(user_id: int | None = None, session: AsyncSession = Depends(get_session)):
    query = select(Calendar).order_by(Calendar.id)
    if user_id is not None:
        query = query.where(Calendar.user_id == user_id)
    return list((await session.scalars(query)).all())


@router.post("/calendars", response_model=CalendarRead, status_code=status.HTTP_201_CREATED)
async def create_calendar(payload: CalendarCreate, session: AsyncSession = Depends(get_session)):
    if not await session.get(User, payload.user_id):
        raise HTTPException(status_code=404, detail="User not found")
    calendar = Calendar(**payload.model_dump())
    session.add(calendar)
    await session.commit()
    await session.refresh(calendar)
    return calendar


@router.get("/calendars/{calendar_id}", response_model=CalendarRead)
async def get_calendar(calendar_id: int, session: AsyncSession = Depends(get_session)):
    calendar = await session.get(Calendar, calendar_id)
    if not calendar:
        raise HTTPException(status_code=404, detail="Calendar not found")
    return calendar


@router.post("/events", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(payload: EventCreate, session: AsyncSession = Depends(get_session)):
    if payload.end_at <= payload.start_at:
        raise HTTPException(status_code=422, detail="end_at must be later than start_at")
    calendar = await session.get(Calendar, payload.calendar_id)
    if not await session.get(User, payload.user_id) or not calendar or calendar.user_id != payload.user_id:
        raise HTTPException(status_code=404, detail="User or calendar not found")
    event = Event(**payload.model_dump())
    session.add(event)
    await session.commit()
    await session.refresh(event)
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == event.user_id, Integration.provider == "google")
    )
    if integration:
        from app.services.google_calendar import GoogleCalendarProvider

        provider = GoogleCalendarProvider(integration.access_token, integration.refresh_token)
        google_event = {
            "summary": event.title,
            "description": event.description,
            "location": event.location,
            "start": {"dateTime": event.start_at.isoformat(), "timeZone": event.timezone},
            "end": {"dateTime": event.end_at.isoformat(), "timeZone": event.timezone},
        }
        try:
            result = await provider.create_event("primary", google_event)
            event.external_id = result.get("id")
            event.sync_status = "synced"
            event.source = "google"
            await session.commit()
            await session.refresh(event)
        except httpx.HTTPError:
            event.sync_status = "error"
            await session.commit()
    return event


@router.get("/events", response_model=list[EventRead])
async def list_events(user_id: int | None = None, session: AsyncSession = Depends(get_session)):
    query = select(Event).order_by(Event.start_at)
    if user_id is not None:
        query = query.where(Event.user_id == user_id)
    return list((await session.scalars(query)).all())


@router.get("/events/{event_id}", response_model=EventRead)
async def get_event(event_id: int, session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.put("/events/{event_id}", response_model=EventRead)
async def update_event(event_id: int, payload: EventUpdate, session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(event, key, value)
    if event.end_at <= event.start_at:
        raise HTTPException(status_code=422, detail="end_at must be later than start_at")
    event.sync_status = "pending"
    await session.commit()
    await session.refresh(event)
    return event


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(event_id: int, session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await session.delete(event)
    await session.commit()


@router.post("/events/{event_id}/sync/google")
async def sync_event_to_google(event_id: int, session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == event.user_id, Integration.provider == "google")
    )
    if not integration:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected")
    from app.services.google_calendar import GoogleCalendarProvider

    provider = GoogleCalendarProvider(integration.access_token, integration.refresh_token)
    calendar = await session.get(Calendar, event.calendar_id)
    calendar_id = calendar.external_id if calendar and calendar.external_id else "primary"
    google_event = {
        "summary": event.title,
        "description": event.description,
        "location": event.location,
        "start": {"dateTime": event.start_at.isoformat(), "timeZone": event.timezone},
        "end": {"dateTime": event.end_at.isoformat(), "timeZone": event.timezone},
    }
    try:
        result = await provider.update_event(calendar_id, event.external_id, google_event) if event.external_id else await provider.create_event(calendar_id, google_event)
    except httpx.HTTPError as error:
        event.sync_status = "error"
        await session.commit()
        raise HTTPException(status_code=502, detail="Google Calendar request failed") from error
    event.external_id = result.get("id")
    event.sync_status = "synced"
    event.source = "google"
    await session.commit()
    await session.refresh(event)
    return event


@router.post("/calendars/{calendar_id}/sync")
async def sync_calendar(calendar_id: int, session: AsyncSession = Depends(get_session)):
    calendar = await session.get(Calendar, calendar_id)
    if not calendar:
        raise HTTPException(status_code=404, detail="Calendar not found")
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == calendar.user_id, Integration.provider == "google")
    )
    if not integration:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected")
    from app.services.google_calendar import GoogleCalendarProvider

    provider = GoogleCalendarProvider(integration.access_token, integration.refresh_token)
    try:
        remote_calendars = await provider.get_calendars()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="Google Calendar request failed") from error
    imported = 0
    for remote in remote_calendars:
        external_id = remote.get("id")
        if not external_id:
            continue
        local = await session.scalar(select(Calendar).where(Calendar.external_id == external_id))
        if not local:
            session.add(Calendar(user_id=calendar.user_id, name=remote.get("summary", external_id), provider="google", external_id=external_id, timezone=remote.get("timeZone", "UTC")))
            imported += 1
    await session.commit()
    return {"imported_calendars": imported, "available_calendars": len(remote_calendars)}


@router.post("/events/{event_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_event_file(event_id: int, file: UploadFile = File(...), session: AsyncSession = Depends(get_session)):
    event = await session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    suffix = Path(file.filename or "").suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Unsupported file type")
    content = await file.read()
    max_size = settings.max_file_size_mb * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="File is too large")
    stored_filename = f"{uuid4().hex}.{suffix}"
    target_dir = settings.storage_path / "events" / str(event_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_filename
    target.write_bytes(content)
    record = EventFile(
        event_id=event_id,
        original_filename=file.filename or stored_filename,
        stored_filename=stored_filename,
        mime_type=file.content_type or "application/octet-stream",
        file_size=len(content),
        storage_path=str(target),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return {"id": record.id, "filename": record.original_filename, "size": record.file_size}


@router.get("/events/{event_id}/files")
async def list_event_files(event_id: int, session: AsyncSession = Depends(get_session)):
    if not await session.get(Event, event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    records = (await session.scalars(select(EventFile).where(EventFile.event_id == event_id))).all()
    return [{"id": item.id, "filename": item.original_filename, "size": item.file_size, "mime_type": item.mime_type} for item in records]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, session: AsyncSession = Depends(get_session)):
    record = await session.get(EventFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    Path(record.storage_path).unlink(missing_ok=True)
    await session.delete(record)
    await session.commit()


auth_router = APIRouter(prefix="/auth/google")


def _oauth_state() -> str:
    payload = urlsafe_b64encode(json.dumps({"expires": int(datetime.now(timezone.utc).timestamp()) + 600}).encode()).decode()
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _validate_oauth_state(state: str) -> None:
    try:
        payload, signature = state.split(".", 1)
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(urlsafe_b64decode(payload.encode()))
        if int(data["expires"]) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError
        return None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state") from None


def _session_token(user_id: int) -> str:
    payload = urlsafe_b64encode(json.dumps({"user_id": user_id}).encode()).decode()
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _user_id_from_session(token: str | None) -> int:
    try:
        payload, signature = (token or "").split(".", 1)
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        return int(json.loads(urlsafe_b64decode(payload.encode()))["user_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Google login required") from None


async def _resolve_user_id(token: str | None, session: AsyncSession) -> int:
    try:
        return _user_id_from_session(token)
    except HTTPException:
        user_id = await session.scalar(
            select(User.id)
            .join(Integration, Integration.user_id == User.id)
            .where(Integration.provider == "google")
            .order_by(User.id.desc())
            .limit(1)
        )
        if user_id is None:
            raise
        return user_id


@auth_router.get("/login")
async def google_login():
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile https://www.googleapis.com/auth/calendar",
            "access_type": "offline",
            "prompt": "consent",
            "state": _oauth_state(),
        }
    )
    return {"authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


@auth_router.get("/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    _validate_oauth_state(state)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.is_error:
        raise HTTPException(status_code=400, detail="Google rejected the authorization code")
    token_data = response.json()
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    if profile_response.is_error or not profile_response.json().get("email"):
        raise HTTPException(status_code=400, detail="Google account email was not returned")
    profile = profile_response.json()
    user = await session.scalar(select(User).where(User.email == profile["email"]))
    if not user:
        user = User(email=profile["email"], name=profile.get("name"))
        session.add(user)
        await session.flush()
    user_id = user.id
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    expires_in = token_data.get("expires_in")
    values = {
        "user_id": user_id,
        "provider": "google",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token") or (integration.refresh_token if integration else None),
        "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        session.add(Integration(**values))
    await session.commit()
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("focus_day_session", _session_token(user.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    response.set_cookie("focus_day_identity", user.email, httponly=False, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@auth_router.post("/disconnect")
async def google_disconnect(user_id: int, session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@router.get("/me", response_model=UserRead)
async def current_user(
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    user_id = await _resolve_user_id(session_token, session)
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Google login required")
    return user


@router.post("/assistant/message", response_model=AssistantResponse)
async def assistant_message(
    payload: AssistantMessage,
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    user_id = await _resolve_user_id(session_token, session)
    if not settings.gigachat_credentials:
        raise HTTPException(status_code=503, detail="GigaChat is not configured")
    from services.gigachat import GigaChatClient

    try:
        result = await GigaChatClient().process_message(payload.text, payload.timezone)
    except Exception as error:
        raise HTTPException(status_code=502, detail="GigaChat is temporarily unavailable") from error
    return AssistantResponse(answer=result.get("answer"), proposed_events=result.get("events", []))


@router.post("/assistant/confirm", response_model=AssistantResponse)
async def confirm_assistant_events(
    payload: AssistantConfirmation,
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    user_id = await _resolve_user_id(session_token, session)
    calendar = await session.scalar(select(Calendar).where(Calendar.user_id == user_id).order_by(Calendar.id))
    if not calendar:
        calendar = Calendar(user_id=user_id, name="Личный календарь", timezone=payload.timezone)
        session.add(calendar)
        await session.flush()
    created_events = []
    for item in payload.events:
        try:
            start_at = datetime.fromisoformat(item["starts_at"])
            end_at = datetime.fromisoformat(item["ends_at"]) if item.get("ends_at") else start_at + timedelta(hours=1)
            event = Event(calendar_id=calendar.id, user_id=user_id, title=str(item["title"])[:300], description=item.get("description"), start_at=start_at, end_at=end_at, timezone=payload.timezone, location=item.get("location"), source="ai")
            session.add(event)
            created_events.append(event)
        except (KeyError, TypeError, ValueError):
            continue
    await session.commit()
    for event in created_events:
        await session.refresh(event)
    integration = await session.scalar(select(Integration).where(Integration.user_id == user_id, Integration.provider == "google"))
    if integration and created_events:
        from app.services.google_calendar import GoogleCalendarProvider

        provider = GoogleCalendarProvider(integration.access_token, integration.refresh_token)
        for event in created_events:
            try:
                remote = await provider.create_event("primary", {"summary": event.title, "description": event.description, "location": event.location, "start": {"dateTime": event.start_at.isoformat(), "timeZone": event.timezone}, "end": {"dateTime": event.end_at.isoformat(), "timeZone": event.timezone}})
                event.external_id = remote.get("id")
                event.sync_status = "synced"
                event.source = "google"
            except httpx.HTTPError:
                event.sync_status = "error"
        await session.commit()
        for event in created_events:
            await session.refresh(event)
    return AssistantResponse(answer="События добавлены.", created_events=created_events)


@router.get("/calendar/export.ics")
async def export_calendar(
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    user_id = await _resolve_user_id(session_token, session)
    events = list((await session.scalars(select(Event).where(Event.user_id == user_id).order_by(Event.start_at))).all())
    from services.calendar import build_calendar

    content = build_calendar(
        [
            {
                "title": event.title,
                "starts_at": event.start_at.isoformat(),
                "ends_at": event.end_at.isoformat() if event.end_at else None,
                "description": event.description,
                "location": event.location,
            }
            for event in events
        ]
    )
    return Response(content=content, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=focus-day.ics"})


@router.post("/assistant/search")
async def assistant_search(
    payload: AssistantMessage,
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    user_id = await _resolve_user_id(session_token, session)
    if not settings.gigachat_credentials:
        raise HTTPException(status_code=503, detail="GigaChat is not configured")
    from services.gigachat import GigaChatClient

    try:
        filters = await GigaChatClient().extract_search_filters(payload.text, payload.timezone)
    except Exception as error:
        raise HTTPException(status_code=502, detail="GigaChat is temporarily unavailable") from error
    conditions = [Event.user_id == user_id]
    if filters.get("date_from"):
        conditions.append(Event.start_at >= datetime.fromisoformat(filters["date_from"]))
    if filters.get("date_to"):
        conditions.append(Event.start_at < datetime.fromisoformat(filters["date_to"]) + timedelta(days=1))
    for keyword in filters.get("keywords", []):
        conditions.append(Event.title.ilike(f"%{keyword}%"))
    events = list((await session.scalars(select(Event).where(*conditions).order_by(Event.start_at))).all())
    return {"filters": filters, "events": events}


@router.post("/files/{file_id}/text")
async def extract_file_text(file_id: int, session: AsyncSession = Depends(get_session)):
    record = await session.get(EventFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    from services.text_extractors import extract_document

    try:
        return {"file_id": file_id, "text": extract_document(record.storage_path)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/assistant/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    suffix = Path(audio.filename or "audio").suffix or ".audio"
    target = settings.storage_path / f"transcription-{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await audio.read())
    try:
        from services.speech import recognize_audio

        return {"text": recognize_audio(str(target))}
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        target.unlink(missing_ok=True)


@router.post("/assistant/file")
async def send_chat_file(
    file: UploadFile = File(...),
    session_token: str | None = Cookie(default=None, alias="focus_day_session"),
    session: AsyncSession = Depends(get_session),
):
    await _resolve_user_id(session_token, session)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(status_code=422, detail="Поддерживаются только PDF и DOCX")
    content = await file.read()
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    target = settings.storage_path / f"chat-{uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        from services.text_extractors import extract_document

        return {"filename": file.filename, "text": extract_document(str(target))}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        target.unlink(missing_ok=True)