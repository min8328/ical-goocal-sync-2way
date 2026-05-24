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
        ("Thursday, January 1, 1970", "날짜: 구버전 epoch (교체 필요)"),
        ("unixToDate", "날짜: AppleScript unixToDate (구버전, 교체 권장)"),
    ]
    print(f"파일: {path}")
    for needle, label in checks:
        found = needle in text
        mark = "있음" if found else "없음"
        print(f"  [{mark}] {label}")
    if "Thursday, January 1, 1970" in text:
        print("\n→ GitHub/PR에서 apple_calendar.py 최신본으로 덮어쓰세요.")
        sys.exit(1)
    if "_mac_date_literal" not in text:
        print("\n→ apple_calendar.py 가 손상되었거나 너무 오래된 버전입니다.")
        sys.exit(1)
    print("\n→ apple_calendar.py 버전은 최신으로 보입니다.")
    sys.exit(0)


if __name__ == "__main__":
    main()
