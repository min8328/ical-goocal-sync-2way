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


def _run_applescript(script: str, timeout_seconds: int = 600) -> str:
    """Mojave 호환: 여러 줄 스크립트는 stdin으로 전달."""
    try:
        proc = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppleCalendarError(
            f"AppleScript 시간 초과 ({timeout_seconds}초). "
            "Calendar.app이 응답하지 않거나 일정이 너무 많습니다. "
            "config.yaml 의 sync_days_past/sync_days_future 를 줄여 보세요."
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise AppleCalendarError(err or "AppleScript failed")
    return (proc.stdout or "").strip()


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _mac_date_literal(dt: datetime) -> str:
    """맥 로케일의 date -r 출력을 AppleScript date 리터럴로 사용."""
    ts = int(dt.timestamp())
    proc = subprocess.run(
        ["date", "-r", str(ts)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AppleCalendarError(
            f"date -r {ts} 실패: {(proc.stderr or proc.stdout).strip()}"
        )
    return _escape_applescript_string(proc.stdout.strip())


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
    timeout_seconds: int = 600,
) -> List[AppleEvent]:
    cal = _escape_applescript_string(calendar_name)
    start_lit = _mac_date_literal(start)
    end_lit = _mac_date_literal(end)

    # 핸들러는 Mojave에서 바깥 변수(fieldSep 등)를 못 봄 → 인라인만 사용
    script = f'''
set fieldSep to (ASCII character 30)
set recordSep to (ASCII character 31)
set rangeStart to date "{start_lit}"
set rangeEnd to date "{end_lit}"
set eventRecords to {{}}

tell application "Calendar"
    if not (exists calendar "{cal}") then
        error "Calendar not found: {cal}"
    end if
    set calRef to calendar "{cal}"
    set matched to every event of calRef whose start date is greater than or equal to rangeStart and start date is less than or equal to rangeEnd
    repeat with ev in matched
        set sd to start date of ev
        set endVal to sd
        try
            set endVal to end date of ev
        end try
        set ad to false
        try
            set ad to allday event of ev
        end try

        set uidText to ""
        try
            set uidText to uid of ev as text
        end try
        set sumText to ""
        try
            set sumText to summary of ev as text
        end try
        set descText to ""
        try
            set descText to description of ev as text
        end try
        set locText to ""
        try
            set locText to location of ev as text
        end try

        set y1 to year of sd
        set mo1 to month of sd as integer
        set d1 to day of sd
        set h1 to hours of sd
        set mi1 to minutes of sd
        set s1 to seconds of sd
        if mo1 < 10 then set mo1t to "0" & mo1 else set mo1t to mo1 as text
        if d1 < 10 then set d1t to "0" & d1 else set d1t to d1 as text
        if h1 < 10 then set h1t to "0" & h1 else set h1t to h1 as text
        if mi1 < 10 then set mi1t to "0" & mi1 else set mi1t to mi1 as text
        if s1 < 10 then set s1t to "0" & s1 else set s1t to s1 as text
        set startIso to (y1 as text) & "-" & mo1t & "-" & d1t & "T" & h1t & ":" & mi1t & ":" & s1t

        set y2 to year of endVal
        set mo2 to month of endVal as integer
        set d2 to day of endVal
        set h2 to hours of endVal
        set mi2 to minutes of endVal
        set s2 to seconds of endVal
        if mo2 < 10 then set mo2t to "0" & mo2 else set mo2t to mo2 as text
        if d2 < 10 then set d2t to "0" & d2 else set d2t to d2 as text
        if h2 < 10 then set h2t to "0" & h2 else set h2t to h2 as text
        if mi2 < 10 then set mi2t to "0" & mi2 else set mi2t to mi2 as text
        if s2 < 10 then set s2t to "0" & s2 else set s2t to s2 as text
        set endIso to (y2 as text) & "-" & mo2t & "-" & d2t & "T" & h2t & ":" & mi2t & ":" & s2t

        set oneLine to uidText & fieldSep & sumText & fieldSep & descText & fieldSep & locText & fieldSep & startIso & fieldSep & endIso & fieldSep & (ad as text)
        set end of eventRecords to oneLine
    end repeat
end tell

set AppleScript's text item delimiters to recordSep
set outText to eventRecords as text
set AppleScript's text item delimiters to ""
return outText
'''

    raw = _run_applescript(script, timeout_seconds=timeout_seconds)
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
    s_lit = _mac_date_literal(start)
    e_lit = _mac_date_literal(end)

    script = f'''
set s to date "{s_lit}"
set e to date "{e_lit}"
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
    s_lit = _mac_date_literal(start)
    e_lit = _mac_date_literal(end)

    script = f'''
set s to date "{s_lit}"
set e to date "{e_lit}"
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
