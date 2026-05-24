#!/usr/bin/env python3
"""Apple Calendar(가족) ↔ Google Calendar 양방향 동기화 데몬."""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import yaml

import apple_calendar as apple
import apple_calendar_ical as apple_ical
import google_calendar as google
from apple_calendar import AppleEvent
from google_calendar import GoogleEvent
from sync_store import LinkRecord, SyncStore

ORIGIN_APPLE = "apple"
ORIGIN_GOOGLE = "google"
META_GOOGLE_SYNC_TOKEN = "google_sync_token"
META_ICAL_BOOTSTRAP = "ical_bootstrap_done"
APPLE_SOURCE_ICAL = "published_ical"
APPLE_SOURCE_APP = "calendar_app"


def expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def load_config(path: str) -> dict:
    cfg_path = expand(path)
    with cfg_path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    required = [
        "apple_calendar_name",
        "google_calendar_id",
        "poll_interval_seconds",
        "sync_days_past",
        "sync_days_future",
        "google_credentials_path",
        "google_token_path",
        "state_db_path",
        "log_path",
    ]
    for key in required:
        if key not in cfg:
            raise KeyError(f"config.yaml 에 '{key}' 가 필요합니다.")
    cfg.setdefault("sync_marker", "[AGS]")
    cfg.setdefault("apple_source", APPLE_SOURCE_ICAL)
    cfg.setdefault("apple_script_timeout_seconds", 600)
    cfg.setdefault("apple_sync_chunk_days", 14)
    cfg.setdefault("apple_event_timeout_seconds", 1200)
    cfg.setdefault("ical_request_timeout_seconds", 30)
    cfg.setdefault("ical_bootstrap_import", False)

    source = cfg["apple_source"]
    if source == APPLE_SOURCE_ICAL:
        if not cfg.get("apple_published_url"):
            raise KeyError(
                "apple_source 가 published_ical 이면 apple_published_url 이 필요합니다."
            )
    elif source != APPLE_SOURCE_APP:
        raise KeyError(
            f"apple_source 는 {APPLE_SOURCE_ICAL} 또는 {APPLE_SOURCE_APP} 이어야 합니다."
        )
    return cfg


def setup_logging(log_path: str) -> None:
    log_file = expand(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def log_info(message: str, *args) -> None:
    logging.info(message, *args)
    for handler in logging.root.handlers:
        handler.flush()


def sync_window(cfg: dict) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=int(cfg["sync_days_past"]))
    future = now + timedelta(days=int(cfg["sync_days_future"]))
    return past, future


def with_marker(description: str, marker: str) -> str:
    if not marker:
        return description
    if marker in description:
        return description
    if description:
        return f"{marker}\n{description}"
    return marker


def strip_marker(description: str, marker: str) -> str:
    if not marker:
        return description
    lines = [ln for ln in description.splitlines() if ln.strip() != marker]
    return "\n".join(lines).strip()


class CalendarSyncDaemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = SyncStore(cfg["state_db_path"])
        self.service = google.get_service(
            cfg["google_credentials_path"], cfg["google_token_path"]
        )
        self.apple_cal = cfg["apple_calendar_name"]
        self.google_cal = cfg["google_calendar_id"]
        self.marker = cfg.get("sync_marker", "")

    def close(self) -> None:
        self.store.close()

    def _apple_source(self) -> str:
        return self.cfg.get("apple_source", APPLE_SOURCE_ICAL)

    def _fetch_apple_events(
        self, time_min: datetime, time_max: datetime
    ) -> Dict[str, AppleEvent]:
        source = self._apple_source()
        if source == APPLE_SOURCE_ICAL:
            log_info("iCal 게시 URL에서 '%s' 일정 읽는 중…", self.apple_cal)
            events = apple_ical.list_events(
                self.cfg["apple_published_url"],
                time_min,
                time_max,
                timeout_seconds=int(self.cfg.get("ical_request_timeout_seconds", 30)),
            )
        else:
            timeout = int(self.cfg.get("apple_script_timeout_seconds", 600))
            log_info(
                "Apple 캘린더 '%s' 일정 읽는 중… (Calendar.app 응답 대기)",
                self.apple_cal,
            )
            events = apple.list_events(
                self.apple_cal,
                time_min,
                time_max,
                timeout_seconds=timeout,
                chunk_days=int(self.cfg.get("apple_sync_chunk_days", 14)),
                ae_timeout_seconds=int(
                    self.cfg.get("apple_event_timeout_seconds", 1200)
                ),
            )
        by_uid = {e.uid: e for e in events if e.uid}
        log_info("Apple 쪽 일정 %d건 (UID %d개)", len(events), len(by_uid))
        return by_uid

    def run_forever(self) -> None:
        interval = int(self.cfg["poll_interval_seconds"])
        logging.info(
            "동기화 시작 — Apple(%s):'%s' ↔ Google:'%s' (주기 %ds)",
            self._apple_source(),
            self.apple_cal,
            self.google_cal,
            interval,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                logging.exception("동기화 사이클 오류")
            time.sleep(interval)

    def run_once(
        self,
        apple_only: bool = False,
        force_bootstrap_import: bool = False,
    ) -> None:
        time_min, time_max = sync_window(self.cfg)

        log_info(
            "동기화 사이클 시작 (범위 %s ~ %s UTC, Apple 소스=%s)",
            time_min.strftime("%Y-%m-%d"),
            time_max.strftime("%Y-%m-%d"),
            self._apple_source(),
        )
        apple_by_uid = self._fetch_apple_events(time_min, time_max)

        skip_apple_to_google = False
        if self._apple_source() == APPLE_SOURCE_ICAL:
            if force_bootstrap_import:
                self.store.set_meta(META_ICAL_BOOTSTRAP, "1")
                log_info("iCal --bootstrap: 기존 일정을 Google에 반영합니다.")
            elif not self.store.get_meta(META_ICAL_BOOTSTRAP):
                self.store.set_meta(META_ICAL_BOOTSTRAP, "1")
                if self.cfg.get("ical_bootstrap_import", False):
                    log_info("iCal 초기 가져오기: 기존 일정을 Google에 반영합니다.")
                else:
                    skip_apple_to_google = True
                    log_info(
                        "iCal 초기 기준선 저장 (%d건). 다음 주기부터 변경분만 Google에 반영합니다.",
                        len(apple_by_uid),
                    )

        if apple_only:
            log_info("Apple 전용 모드 — Google 동기화 생략")
            return

        log_info("Google Calendar 변경분 조회 중…")
        sync_token = self.store.get_meta(META_GOOGLE_SYNC_TOKEN)
        google_events, next_token, _ = google.list_events_incremental(
            self.service,
            self.google_cal,
            sync_token,
            time_min,
            time_max,
        )
        if next_token:
            self.store.set_meta(META_GOOGLE_SYNC_TOKEN, next_token)
        log_info("Google 변경/일정 %d건", len(google_events))

        google_by_id = {g.event_id: g for g in google_events}
        links = self.store.all_links()

        self._apply_google_changes(google_events, apple_by_uid, links)
        if not skip_apple_to_google:
            self._apply_apple_changes(list(apple_by_uid.values()), google_by_id, links)
            self._reconcile_apple_deletions(apple_by_uid, links)
        else:
            log_info("이번 사이클은 iCal 기준선만 저장 — Apple→Google 생략")

    def _apply_google_changes(
        self,
        google_events: List[GoogleEvent],
        apple_by_uid: Dict[str, AppleEvent],
        links: Dict[str, LinkRecord],
    ) -> None:
        for gev in google_events:
            link = self.store.get_by_google(gev.event_id)
            if link and link.last_origin == ORIGIN_APPLE and gev.fingerprint() == link.google_fp:
                continue

            if gev.status == "cancelled":
                if link:
                    if link.apple_uid in apple_by_uid:
                        apple.delete_event(self.apple_cal, link.apple_uid)
                        logging.info("Apple 삭제 (Google 취소): %s", gev.summary)
                    self.store.delete(link.apple_uid)
                continue

            if link:
                aev = apple_by_uid.get(link.apple_uid)
                if aev and aev.fingerprint() == gev.fingerprint():
                    self.store.upsert(
                        link.apple_uid,
                        gev.event_id,
                        gev.ical_uid,
                        aev.fingerprint(),
                        gev.fingerprint(),
                        ORIGIN_GOOGLE,
                    )
                    continue
                ok = apple.update_event(
                    self.apple_cal,
                    link.apple_uid,
                    gev.summary,
                    strip_marker(gev.description, self.marker),
                    gev.location,
                    gev.start,
                    gev.end,
                    gev.all_day,
                )
                if not ok:
                    apple.create_event(
                        self.apple_cal,
                        link.apple_uid,
                        gev.summary,
                        with_marker(gev.description, self.marker),
                        gev.location,
                        gev.start,
                        gev.end,
                        gev.all_day,
                    )
                logging.info("Apple 수정 (Google): %s", gev.summary)
                self.store.upsert(
                    link.apple_uid,
                    gev.event_id,
                    gev.ical_uid,
                    gev.fingerprint(),
                    gev.fingerprint(),
                    ORIGIN_GOOGLE,
                )
                continue

            # 신규 Google → Apple
            apple_uid = gev.ical_uid or str(uuid.uuid4())
            if apple_uid in apple_by_uid:
                continue

            apple.create_event(
                self.apple_cal,
                apple_uid,
                gev.summary,
                with_marker(gev.description, self.marker),
                gev.location,
                gev.start,
                gev.end,
                gev.all_day,
            )
            logging.info("Apple 생성 (Google): %s", gev.summary)
            self.store.upsert(
                apple_uid,
                gev.event_id,
                gev.ical_uid,
                gev.fingerprint(),
                gev.fingerprint(),
                ORIGIN_GOOGLE,
            )

    def _apply_apple_changes(
        self,
        apple_events: List[AppleEvent],
        google_by_id: Dict[str, GoogleEvent],
        links: Dict[str, LinkRecord],
    ) -> None:
        for aev in apple_events:
            link = self.store.get_by_apple(aev.uid)
            if link and link.last_origin == ORIGIN_GOOGLE and aev.fingerprint() == link.apple_fp:
                continue

            desc = with_marker(aev.description, self.marker)

            if link:
                gev = google_by_id.get(link.google_event_id)
                if gev and gev.fingerprint() == aev.fingerprint():
                    self.store.upsert(
                        aev.uid,
                        link.google_event_id,
                        link.google_ical_uid,
                        aev.fingerprint(),
                        gev.fingerprint(),
                        ORIGIN_APPLE,
                    )
                    continue
                updated = google.update_event(
                    self.service,
                    self.google_cal,
                    link.google_event_id,
                    aev.summary,
                    desc,
                    aev.location,
                    aev.start,
                    aev.end,
                    aev.all_day,
                )
                logging.info("Google 수정 (Apple): %s", aev.summary)
                self.store.upsert(
                    aev.uid,
                    updated.event_id,
                    updated.ical_uid,
                    aev.fingerprint(),
                    updated.fingerprint(),
                    ORIGIN_APPLE,
                )
                continue

            ical_uid = aev.uid or str(uuid.uuid4())
            created = google.create_event(
                self.service,
                self.google_cal,
                ical_uid,
                aev.summary,
                desc,
                aev.location,
                aev.start,
                aev.end,
                aev.all_day,
            )
            logging.info("Google 생성 (Apple): %s", aev.summary)
            self.store.upsert(
                aev.uid,
                created.event_id,
                created.ical_uid,
                aev.fingerprint(),
                created.fingerprint(),
                ORIGIN_APPLE,
            )

    def _reconcile_apple_deletions(
        self,
        apple_by_uid: Dict[str, AppleEvent],
        links: Dict[str, LinkRecord],
    ) -> None:
        for apple_uid, link in list(links.items()):
            if apple_uid in apple_by_uid:
                continue
            if link.last_origin == ORIGIN_GOOGLE:
                # 방금 Google에서 만든/수정한 직후 Apple 반영 전일 수 있음 — 한 사이클 유예
                continue
            try:
                google.delete_event(
                    self.service, self.google_cal, link.google_event_id
                )
                logging.info("Google 삭제 (Apple 없음): %s", link.google_event_id)
            except Exception:
                logging.exception("Google 삭제 실패: %s", link.google_event_id)
            self.store.delete(apple_uid)


def run_auth_only(cfg: dict) -> None:
    google.get_service(cfg["google_credentials_path"], cfg["google_token_path"])
    logging.info("Google OAuth 토큰 저장 완료: %s", expand(cfg["google_token_path"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Apple ↔ Google Calendar sync daemon")
    parser.add_argument(
        "-c",
        "--config",
        default=str(Path(__file__).with_name("config.yaml")),
        help="config.yaml 경로",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="한 번만 동기화하고 종료 (테스트용)",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Google OAuth만 수행하고 종료",
    )
    parser.add_argument(
        "--apple-only",
        action="store_true",
        help="Apple 일정 읽기만 테스트 (Google 생략, 1회 실행)",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="iCal 모드: 첫 실행에 기존 일정 전체를 Google에 반영 (1회)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["log_path"])

    if args.auth:
        run_auth_only(cfg)
        return

    daemon = CalendarSyncDaemon(cfg)
    try:
        if args.once or args.apple_only or args.bootstrap:
            daemon.run_once(
                apple_only=args.apple_only,
                force_bootstrap_import=args.bootstrap,
            )
            log_info("단일 동기화 완료")
        else:
            daemon.run_forever()
    finally:
        daemon.close()


if __name__ == "__main__":
    main()
