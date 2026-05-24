"""Apple Calendar.app 접근 (AppleScript, macOS 10.14+)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

# 필드 구분자 (일정 제목/본문에 거의 안 나오는 ASCII 제어문자)
FIELD_SEP = "\x1e"
RECORD_SEP = "\x1f"


class AppleCalendarError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppleEvent:
    uid: str
    summary: str
    description: str
    location: str
    start: datetime
    end: datetime
    all_day: bool

    def fingerprint(self) -> str:
        return "|".join(
            [
                self.summary,
                self.description,
                self.location,
                self.start.isoformat(),
                self.end.isoformat(),
                "1" if self.all_day else "0",
            ]
        )


def _run_applescript(script: str) -> str:
    """Mojave 호환: 여러 줄 스크립트는 stdin으로 전달."""
    proc = subprocess.run(
        ["osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise AppleCalendarError(err or "AppleScript failed")
    return (proc.stdout or "").strip()


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# 한국어 macOS 등에서 영문 epoch 문자열이 파싱되지 않음 → shell date -r 사용
_APPLESCRIPT_UNIX_TO_DATE = """
on unixToDate(sec)
    set ds to do shell script "date -r " & (sec as integer) & " '+%Y-%m-%d %H:%M:%S'"
    try
        return date ds
    on error
        set ds2 to do shell script "date -r " & (sec as integer)
        return date ds2
    end try
end unixToDate
"""


def _parse_apple_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_record_line(line: str) -> AppleEvent:
    parts = line.split(FIELD_SEP)
    if len(parts) < 7:
        raise AppleCalendarError(f"잘못된 Apple 이벤트 레코드: {line[:120]}")
    uid, summary, description, location, start_s, end_s, all_day_s = parts[:7]
    return AppleEvent(
        uid=uid,
        summary=summary,
        description=description,
        location=location,
        start=_parse_apple_datetime(start_s),
        end=_parse_apple_datetime(end_s),
        all_day=all_day_s.lower() in ("true", "1", "yes"),
    )


def list_events(
    calendar_name: str,
    start: datetime,
    end: datetime,
) -> List[AppleEvent]:
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())
    cal = _escape_applescript_string(calendar_name)

    script = f'''
set fieldSep to (ASCII character 30)
set recordSep to (ASCII character 31)

on pad(n)
    if n < 10 then return "0" & (n as text)
    return n as text
end pad

on isoDate(d)
    set y to year of d
    set m to pad(month of d as integer)
    set dd to pad(day of d)
    set h to pad(hours of d)
    set mi to pad(minutes of d)
    set s to pad(seconds of d)
    return (y as text) & "-" & m & "-" & dd & "T" & h & ":" & mi & ":" & s
end isoDate

on asText(v)
    try
        return v as text
    on error
        return ""
    end try
end asText

on cleanField(t)
    set t to my asText(t)
    if t contains fieldSep then
        set AppleScript's text item delimiters to fieldSep
        set t to text items of t as text
        set AppleScript's text item delimiters to ""
    end if
    if t contains recordSep then
        set AppleScript's text item delimiters to recordSep
        set t to text items of t as text
        set AppleScript's text item delimiters to ""
    end if
  return t
end cleanField

{_APPLESCRIPT_UNIX_TO_DATE}
set rangeStart to my unixToDate({start_unix})
set rangeEnd to my unixToDate({end_unix})

set lines to {{}}

tell application "Calendar"
    if not (exists calendar "{cal}") then
        error "Calendar not found: {cal}"
    end if
    set calRef to calendar "{cal}"
    repeat with ev in (every event of calRef)
        set sd to start date of ev
        if sd is greater than or equal to rangeStart and sd is less than or equal to rangeEnd then
            set endVal to sd
            try
                set endVal to end date of ev
            end try
            set ad to false
            try
                set ad to allday event of ev
            end try
            set oneLine to my cleanField(uid of ev) & fieldSep & my cleanField(summary of ev) & fieldSep & my cleanField(description of ev) & fieldSep & my cleanField(location of ev) & fieldSep & my isoDate(sd) & fieldSep & my isoDate(endVal) & fieldSep & (ad as text)
            set end of lines to oneLine
        end if
    end repeat
end tell

set AppleScript's text item delimiters to recordSep
set outText to lines as text
set AppleScript's text item delimiters to ""
return outText
'''

    raw = _run_applescript(script)
    if not raw:
        return []

    events: List[AppleEvent] = []
    for record in raw.split(RECORD_SEP):
        record = record.strip()
        if not record:
            continue
        events.append(_parse_record_line(record))
    return events


def create_event(
    calendar_name: str,
    uid: str,
    summary: str,
    description: str,
    location: str,
    start: datetime,
    end: datetime,
    all_day: bool,
) -> None:
    cal = _escape_applescript_string(calendar_name)
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())

    script = f'''
{_APPLESCRIPT_UNIX_TO_DATE}
set s to my unixToDate({start_unix})
set e to my unixToDate({end_unix})
tell application "Calendar"
    tell calendar "{cal}"
        set ev to make new event with properties {{summary:"{_escape_applescript_string(summary)}", description:"{_escape_applescript_string(description)}", location:"{_escape_applescript_string(location)}", allday event:{str(all_day).lower()}, start date:s, end date:e}}
        try
            set uid of ev to "{_escape_applescript_string(uid)}"
        end try
    end tell
end tell
'''
    _run_applescript(script)


def update_event(
    calendar_name: str,
    uid: str,
    summary: str,
    description: str,
    location: str,
    start: datetime,
    end: datetime,
    all_day: bool,
) -> bool:
    cal = _escape_applescript_string(calendar_name)
    uid_esc = _escape_applescript_string(uid)
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())

    script = f'''
{_APPLESCRIPT_UNIX_TO_DATE}
set s to my unixToDate({start_unix})
set e to my unixToDate({end_unix})
set found to false
tell application "Calendar"
    tell calendar "{cal}"
        repeat with ev in events
            try
                if (uid of ev as text) is equal to "{uid_esc}" then
                    set summary of ev to "{_escape_applescript_string(summary)}"
                    set description of ev to "{_escape_applescript_string(description)}"
                    set location of ev to "{_escape_applescript_string(location)}"
                    set allday event of ev to {str(all_day).lower()}
                    set start date of ev to s
                    set end date of ev to e
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
    end tell
end tell
return found
'''
    result = _run_applescript(script)
    return result.lower() == "true"


def delete_event(calendar_name: str, uid: str) -> bool:
    cal = _escape_applescript_string(calendar_name)
    uid_esc = _escape_applescript_string(uid)

    script = f'''
set found to false
tell application "Calendar"
    tell calendar "{cal}"
        repeat with ev in events
            try
                if (uid of ev as text) is equal to "{uid_esc}" then
                    delete ev
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
    end tell
end tell
return found
'''
    result = _run_applescript(script)
    return result.lower() == "true"
