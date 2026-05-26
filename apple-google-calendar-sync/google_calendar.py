"""Google Calendar API v3 (OAuth, incremental sync)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# sync_daemon 이 config 에서 설정 (Google API timeZone 필드용 IANA 이름)
CALENDAR_TIMEZONE = "Asia/Seoul"


def set_timezone(iana_name: str) -> None:
    global CALENDAR_TIMEZONE
    CALENDAR_TIMEZONE = iana_name or "Asia/Seoul"


class GoogleCalendarError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleEvent:
    event_id: str
    ical_uid: str
    summary: str
    description: str
    location: str
    start: datetime
    end: datetime
    all_day: bool
    updated: datetime
    status: str  # confirmed | cancelled

    def fingerprint(self) -> str:
        return "|".join(
            [
                self.summary,
                self.description,
                self.location,
                self.start.isoformat(),
                self.end.isoformat(),
                "1" if self.all_day else "0",
                self.status,
            ]
        )


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def get_service(credentials_path: str, token_path: str):
    cred_file = _expand(credentials_path)
    token_file = _expand(token_path)

    creds: Optional[Credentials] = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_file.exists():
                raise GoogleCalendarError(
                    f"credentials 파일이 없습니다: {cred_file}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _zone() -> ZoneInfo:
    return ZoneInfo(CALENDAR_TIMEZONE)


def _parse_google_dt(value: Dict, all_day: bool) -> datetime:
    raw = value.get("dateTime") or value.get("date")
    if all_day:
        y, m, d = (int(x) for x in raw.split("-"))
        local = datetime(y, m, d, 0, 0, 0, tzinfo=_zone())
        return local.astimezone(timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone())
    return dt.astimezone(timezone.utc)


def _event_from_api(item: Dict) -> GoogleEvent:
    start_obj = item.get("start", {})
    end_obj = item.get("end", {})
    all_day = "date" in start_obj
    updated_raw = item.get("updated", "1970-01-01T00:00:00Z")
    if updated_raw.endswith("Z"):
        updated_raw = updated_raw[:-1] + "+00:00"
    updated = datetime.fromisoformat(updated_raw).astimezone(timezone.utc)

    start = _parse_google_dt(start_obj, all_day)
    end = _parse_google_dt(end_obj, all_day)
    if not all_day and end <= start:
        end = start + timedelta(hours=1)

    return GoogleEvent(
        event_id=item["id"],
        ical_uid=item.get("iCalUID") or item["id"],
        summary=item.get("summary") or "",
        description=item.get("description") or "",
        location=item.get("location") or "",
        start=start,
        end=end,
        all_day=all_day,
        updated=updated,
        status=item.get("status", "confirmed"),
    )


def list_events_incremental(
    service,
    calendar_id: str,
    sync_token: Optional[str],
    time_min: datetime,
    time_max: datetime,
) -> Tuple[List[GoogleEvent], Optional[str], bool]:
    """Returns (events, next_sync_token, full_sync_reset)."""
    events: List[GoogleEvent] = []
    page_token: Optional[str] = None
    next_sync: Optional[str] = None

    while True:
        kwargs: Dict = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 2500,
        }
        if sync_token:
            kwargs["syncToken"] = sync_token
        else:
            kwargs["timeMin"] = time_min.isoformat()
            kwargs["timeMax"] = time_max.isoformat()

        if page_token:
            kwargs["pageToken"] = page_token

        try:
            resp = service.events().list(**kwargs).execute()
        except HttpError as err:
            if err.resp.status == 410 and sync_token:
                logging.warning("Google sync token 만료 — 전체 동기화로 재시도")
                return list_events_incremental(
                    service, calendar_id, None, time_min, time_max
                )
            raise GoogleCalendarError(str(err)) from err

        for item in resp.get("items", []):
            events.append(_event_from_api(item))

        page_token = resp.get("nextPageToken")
        if not page_token:
            next_sync = resp.get("nextSyncToken")
            break

    full_reset = sync_token is None
    return events, next_sync, full_reset


def _google_time_fields(start: datetime, end: datetime, all_day: bool) -> Dict:
    tz = _zone()
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    if all_day:
        start_local = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = end_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_local <= start_local:
            end_local = start_local + timedelta(days=1)
        return {
            "start": {"date": start_local.strftime("%Y-%m-%d")},
            "end": {"date": end_local.strftime("%Y-%m-%d")},
        }
    if end_local <= start_local:
        end_local = start_local + timedelta(hours=1)
    return {
        "start": {
            "dateTime": start_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": CALENDAR_TIMEZONE,
        },
        "end": {
            "dateTime": end_local.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": CALENDAR_TIMEZONE,
        },
    }


def create_event(
    service,
    calendar_id: str,
    ical_uid: str,
    summary: str,
    description: str,
    location: str,
    start: datetime,
    end: datetime,
    all_day: bool,
) -> GoogleEvent:
    body: Dict = {
        "summary": summary,
        "description": description,
        "location": location,
        "iCalUID": ical_uid,
    }
    body.update(_google_time_fields(start, end, all_day))

    created = service.events().insert(calendarId=calendar_id, body=body).execute()
    return _event_from_api(created)


def update_event(
    service,
    calendar_id: str,
    event_id: str,
    summary: str,
    description: str,
    location: str,
    start: datetime,
    end: datetime,
    all_day: bool,
) -> GoogleEvent:
    existing = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    existing["summary"] = summary
    existing["description"] = description
    existing["location"] = location
    existing.update(_google_time_fields(start, end, all_day))

    updated = (
        service.events()
        .update(calendarId=calendar_id, eventId=event_id, body=existing)
        .execute()
    )
    return _event_from_api(updated)


def delete_event(service, calendar_id: str, event_id: str) -> None:
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
