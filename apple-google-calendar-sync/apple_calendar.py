"""Apple Calendar.app 접근 (AppleScript, macOS 10.14+)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


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
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AppleCalendarError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_apple_datetime(value: str) -> datetime:
    # AppleScript JSONBridge ISO 형식 (대부분 UTC Z 또는 오프셋)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def list_events(
    calendar_name: str,
    start: datetime,
    end: datetime,
) -> List[AppleEvent]:
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())
    cal = _escape_applescript_string(calendar_name)

    script = f'''
    set epochDate to date "Thursday, January 1, 1970 00:00:00"
    set rangeStart to epochDate + {start_unix}
    set rangeEnd to epochDate + {end_unix}

    on isoDate(d)
        set y to year of d
        set m to my pad(month of d as integer)
        set dd to my pad(day of d)
        set h to my pad(hours of d)
        set mi to my pad(minutes of d)
        set s to my pad(seconds of d)
        return (y as text) & "-" & m & "-" & dd & "T" & h & ":" & mi & ":" & s
    end isoDate

    on pad(n)
        if n < 10 then return "0" & (n as text)
        return n as text
    end pad

    on esc(t)
        set t to t as text
        set AppleScript's text item delimiters to "\\"
        set parts to text items of t
        set AppleScript's text item delimiters to "\\\\"
        set t to parts as text
        set AppleScript's text item delimiters to "\\""
        set parts to text items of t
        set AppleScript's text item delimiters to "\\\\\\""
        set t to parts as text
        set AppleScript's text item delimiters to ""
        return t
    end esc

    tell application "Calendar"
        if not (exists calendar "{cal}") then
            error "Calendar not found: {cal}"
        end if
        set calRef to calendar "{cal}"
        set matched to every event of calRef whose start date ≥ rangeStart and start date ≤ rangeEnd
        set jsonItems to {{}}
        repeat with ev in matched
            set endVal to missing value
            try
                set endVal to end date of ev
            end try
            if endVal is missing value then
                set endVal to start date of ev
            end if
            set itemText to "{{\\"uid\\":\\"" & my esc(uid of ev as text) & "\\",\\"summary\\":\\"" & my esc(summary of ev as text) & "\\",\\"description\\":\\"" & my esc(description of ev as text) & "\\",\\"location\\":\\"" & my esc(location of ev as text) & "\\",\\"start\\":\\"" & my isoDate(start date of ev) & "\\",\\"end\\":\\"" & my isoDate(endVal) & "\\",\\"all_day\\":" & (allday event of ev as text) & "}}"
            set end of jsonItems to itemText
        end repeat
    end tell

    set AppleScript's text item delimiters to ","
    set body to jsonItems as text
    set AppleScript's text item delimiters to ""
    return "[" & body & "]"
    '''

    raw = _run_applescript(script)
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppleCalendarError(f"AppleScript JSON parse failed: {raw[:300]}") from exc

    events: List[AppleEvent] = []
    for item in payload:
        start_dt = _parse_apple_datetime(item["start"])
        end_dt = _parse_apple_datetime(item["end"])
        events.append(
            AppleEvent(
                uid=item["uid"],
                summary=item.get("summary") or "",
                description=item.get("description") or "",
                location=item.get("location") or "",
                start=start_dt,
                end=end_dt,
                all_day=bool(item.get("all_day")),
            )
        )
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
    props = [
        f'summary:"{_escape_applescript_string(summary)}"',
        f'description:"{_escape_applescript_string(description)}"',
        f'location:"{_escape_applescript_string(location)}"',
        f"allday event:{str(all_day).lower()}",
    ]

    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())

    script = f'''
    set epochDate to date "Thursday, January 1, 1970 00:00:00"
    set s to epochDate + {start_unix}
    set e to epochDate + {end_unix}
    tell application "Calendar"
        tell calendar "{cal}"
            set ev to make new event with properties {{{", ".join(props)}, start date:s, end date:e}}
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
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())

    script = f'''
    set epochDate to date "Thursday, January 1, 1970 00:00:00"
    set s to epochDate + {start_unix}
    set e to epochDate + {end_unix}
    set found to false
    tell application "Calendar"
        tell calendar "{cal}"
            repeat with ev in (every event)
                if (uid of ev as text) is "{_escape_applescript_string(uid)}" then
                    set summary of ev to "{_escape_applescript_string(summary)}"
                    set description of ev to "{_escape_applescript_string(description)}"
                    set location of ev to "{_escape_applescript_string(location)}"
                    set allday event of ev to {str(all_day).lower()}
                    set start date of ev to s
                    set end date of ev to e
                    set found to true
                    exit repeat
                end if
            end repeat
        end tell
    end tell
    return found
    '''
    result = _run_applescript(script)
    return result.lower() == "true"


def delete_event(calendar_name: str, uid: str) -> bool:
    cal = _escape_applescript_string(calendar_name)
    script = f'''
    set found to false
    tell application "Calendar"
        tell calendar "{cal}"
            repeat with ev in (every event)
                if (uid of ev as text) is "{_escape_applescript_string(uid)}" then
                    delete ev
                    set found to true
                    exit repeat
                end if
            end repeat
        end tell
    end tell
    return found
    '''
    result = _run_applescript(script)
    return result.lower() == "true"
