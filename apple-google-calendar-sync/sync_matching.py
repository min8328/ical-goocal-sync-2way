"""AppleEvent ↔ GoogleEvent 매칭 (중복 생성 방지)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from apple_calendar import AppleEvent
from google_calendar import GoogleEvent


def normalize_summary(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip().lower())
    return re.sub(r"\s+", " ", t)


def _start_key_all_day(dt) -> str:
    return dt.strftime("%Y-%m-%d")


def events_match(
    apple: AppleEvent,
    google: GoogleEvent,
    tolerance_minutes: int = 5,
) -> bool:
    if normalize_summary(apple.summary) != normalize_summary(google.summary):
        return False
    if apple.all_day != google.all_day:
        return False
    if apple.all_day:
        return _start_key_all_day(apple.start) == _start_key_all_day(google.start)
    delta = abs(apple.start - google.start)
    return delta <= timedelta(minutes=tolerance_minutes)


def find_apple_for_google(
    google: GoogleEvent,
    apple_by_uid: Dict[str, AppleEvent],
    tolerance_minutes: int = 5,
) -> Optional[AppleEvent]:
    if google.ical_uid and google.ical_uid in apple_by_uid:
        return apple_by_uid[google.ical_uid]
    if tolerance_minutes <= 0:
        return None
    for aev in apple_by_uid.values():
        if events_match(aev, google, tolerance_minutes):
            return aev
    return None


def find_google_for_apple(
    apple: AppleEvent,
    google_by_ical_uid: Dict[str, GoogleEvent],
    google_events: List[GoogleEvent],
    tolerance_minutes: int = 5,
) -> Optional[GoogleEvent]:
    if apple.uid and apple.uid in google_by_ical_uid:
        return google_by_ical_uid[apple.uid]
    if tolerance_minutes <= 0:
        return None
    for gev in google_events:
        if gev.status == "cancelled":
            continue
        if events_match(apple, gev, tolerance_minutes):
            return gev
    return None


def has_sync_marker(description: str, marker: str) -> bool:
    if not marker:
        return False
    return marker in (description or "")


def core_fields_differ(
    apple: AppleEvent,
    google: GoogleEvent,
    tolerance_minutes: int = 5,
) -> bool:
    """제목·시각·종일·장소만 비교 (메모/UID 차이는 무시)."""
    if normalize_summary(apple.summary) != normalize_summary(google.summary):
        return True
    if apple.all_day != google.all_day:
        return True
    if apple.all_day:
        if _start_key_all_day(apple.start) != _start_key_all_day(google.start):
            return True
    elif abs(apple.start - google.start) > timedelta(minutes=tolerance_minutes):
        return True
    al = (apple.location or "").strip()
    gl = (google.location or "").strip()
    if al and gl and al != gl:
        return True
    return False
