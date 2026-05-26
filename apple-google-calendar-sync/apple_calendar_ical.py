"""iCloud 게시(공개) 캘린더 ICS URL → AppleEvent 목록 (Calendar.app 불필요)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Union
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from apple_calendar import AppleEvent, AppleCalendarError
from google_calendar import CALENDAR_TIMEZONE

logger = logging.getLogger(__name__)


def normalize_published_url(url: str) -> str:
    u = url.strip()
    if u.startswith("webcal://"):
        return "https://" + u[len("webcal://") :]
    if u.startswith("webcal:"):
        return "https:" + u[len("webcal:") :]
    return u


def _calendar_tz() -> ZoneInfo:
    return ZoneInfo(CALENDAR_TIMEZONE)


def _to_utc(value: Union[date, datetime]) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_calendar_tz())
        return value.astimezone(timezone.utc)
    local = datetime(value.year, value.month, value.day, tzinfo=_calendar_tz())
    return local.astimezone(timezone.utc)


def _is_all_day(component) -> bool:
    dtstart = component.get("dtstart")
    if dtstart is None:
        return False
    param = dtstart.params.get("VALUE") if hasattr(dtstart, "params") else None
    if param == "DATE":
        return True
    raw = dtstart.dt
    return isinstance(raw, date) and not isinstance(raw, datetime)


def _revision(component) -> str:
    lm = component.get("last-modified")
    if lm is None:
        return ""
    try:
        return _to_utc(lm.dt).isoformat()
    except Exception:
        return str(lm.dt)


def list_events(
    published_url: str,
    time_min: datetime,
    time_max: datetime,
    timeout_seconds: int = 30,
) -> List[AppleEvent]:
    """게시 ICS에서 일정을 읽습니다. start 가 [time_min, time_max] 안인 것만."""
    url = normalize_published_url(published_url)
    if not url:
        raise AppleCalendarError("apple_published_url 이 비어 있습니다.")

    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise AppleCalendarError(f"iCal 다운로드 실패: {exc}") from exc

    if response.status_code != 200:
        raise AppleCalendarError(
            f"iCal 다운로드 HTTP {response.status_code}: {url[:80]}..."
        )

    try:
        cal = Calendar.from_ical(response.content)
    except Exception as exc:
        raise AppleCalendarError(f"iCal 파싱 실패: {exc}") from exc

    events: List[AppleEvent] = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid_raw = component.get("uid")
        if not uid_raw:
            continue
        uid = str(uid_raw)

        dtstart = component.get("dtstart")
        if dtstart is None:
            continue

        all_day = _is_all_day(component)
        start_raw = dtstart.dt
        start = _to_utc(start_raw)

        if start < time_min or start > time_max:
            continue

        if component.get("dtend"):
            end_raw = component.get("dtend").dt
            end = _to_utc(end_raw)
            # 종일 DTEND 는 RFC5545 기준 익일(배타) — 추가 +1일 하면 이틀짜리로 깨짐
            if all_day and isinstance(end_raw, date) and end <= start:
                end = start + timedelta(days=1)
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

        summary = str(component.get("summary") or "제목 없음")
        description = str(component.get("description") or "")
        location = str(component.get("location") or "")

        events.append(
            AppleEvent(
                uid=uid,
                summary=summary,
                description=description,
                location=location,
                start=start,
                end=end,
                all_day=all_day,
                revision=_revision(component),
            )
        )

    logger.info("iCal 게시 URL에서 %d건 로드 (범위 내)", len(events))
    return events
