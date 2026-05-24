#!/usr/bin/env python3
"""맥에서 apple_calendar.py 최신 설치 여부 확인."""

from __future__ import annotations

import pathlib
import sys


def main() -> None:
    path = pathlib.Path(__file__).with_name("apple_calendar.py")
    text = path.read_text(encoding="utf-8")
    checks = [
        ("_mac_date_literal", "날짜: 맥 date -r 방식 (최신)"),
        ("eventRecords", "변수: lines 예약어 충돌 수정 (최신)"),
        ("set uidText to", "AppleScript: 핸들러 없는 인라인 버전 (최신)"),
        ("year of sd as text", "AppleScript: 숫자 시각 컴포넌트 방식 (최신)"),
        ("else set mo1t", "AppleScript: Mojave에서 실패하는 한줄 if/else (교체 필요)"),
        ("on cleanField(t)", "AppleScript: 구버전 핸들러 (교체 필요)"),
        ("Thursday, January 1, 1970", "날짜: 구버전 epoch (교체 필요)"),
        ("set lines to", "변수: lines 예약어 (교체 필요)"),
        ("unixToDate", "날짜: AppleScript unixToDate (구버전, 교체 권장)"),
    ]
    print(f"파일: {path}")
    for needle, label in checks:
        found = needle in text
        mark = "있음" if found else "없음"
        print(f"  [{mark}] {label}")
    if "Thursday, January 1, 1970" in text or "set lines to" in text:
        print("\n→ GitHub/PR에서 apple_calendar.py 최신본으로 덮어쓰세요.")
        sys.exit(1)
    if "_mac_date_literal" not in text or "eventRecords" not in text:
        print("\n→ apple_calendar.py 가 손상되었거나 너무 오래된 버전입니다.")
        sys.exit(1)
    if "on cleanField(t)" in text:
        print("\n→ fieldSep 핸들러 버그가 있는 구버전입니다. 최신 apple_calendar.py 로 교체하세요.")
        sys.exit(1)
    if "set uidText to" not in text or "year of sd as text" not in text:
        print("\n→ AppleScript 인라인 버전이 아닙니다. 최신 apple_calendar.py 로 교체하세요.")
        sys.exit(1)
    if "else set mo1t" in text:
        print("\n→ Mojave 비호환 한줄 if/else 가 있습니다. 최신 apple_calendar.py 로 교체하세요.")
        sys.exit(1)
    print("\n→ apple_calendar.py 버전은 최신으로 보입니다.")
    sys.exit(0)


if __name__ == "__main__":
    main()
