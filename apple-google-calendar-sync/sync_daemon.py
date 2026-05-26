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
from typing import Dict, List, Optional

import yaml

import apple_calendar as apple
import apple_calendar_ical as apple_ical
import google_calendar as google
from apple_calendar import AppleEvent
from google_calendar import GoogleEvent
from sync_matching import (
    core_fields_differ,
    events_match,
    find_apple_for_google,
    find_google_for_apple,
    has_sync_marker,
    normalize_all_day_bounds,
)
from sync_store import LinkRecord, SyncStore

ORIGIN_APPLE = "apple"
ORIGIN_GOOGLE = "google"
META_GOOGLE_SYNC_TOKEN = "google_sync_token"
META_ICAL_BOOTSTRAP = "ical_bootstrap_done"
APPLE_SOURCE_ICAL = "published_ical"
APPLE_SOURCE_APP = "calendar_app"
# Google→Apple 직후 iCal 게시 URL에 UID가 안 잡히는 지연
ICAL_PUBLISH_GRACE = timedelta(minutes=15)


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
    cfg.setdefault("fuzzy_match_enabled", True)
    cfg.setdefault("fuzzy_match_tolerance_minutes", 5)
    cfg.setdefault("google_full_list_each_cycle", True)
    cfg.setdefault("apple_script_search_pad_days", 3)

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
        google.set_timezone(cfg.get("timezone", "Asia/Seoul"))
        apple.set_search_pad_days(int(cfg.get("apple_script_search_pad_days", 3)))

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

        sync_token = self.store.get_meta(META_GOOGLE_SYNC_TOKEN)
        if self.cfg.get("google_full_list_each_cycle", True):
            google_all, _, _ = google.list_events_incremental(
                self.service,
                self.google_cal,
                None,
                time_min,
                time_max,
            )
            log_info("Google 전체 스캔 %d건 (매칭용)", len(google_all))
        else:
            google_all = []

        log_info("Google Calendar 변경분 조회 중…")
        google_delta, next_token, _ = google.list_events_incremental(
            self.service,
            self.google_cal,
            sync_token,
            time_min,
            time_max,
        )
        if next_token:
            self.store.set_meta(META_GOOGLE_SYNC_TOKEN, next_token)
        log_info("Google 변경분 %d건", len(google_delta))

        google_for_match = google_all if google_all else google_delta
        google_by_id = {g.event_id: g for g in google_for_match}
        google_by_ical_uid: Dict[str, GoogleEvent] = {}
        for g in google_for_match:
            if g.ical_uid:
                google_by_ical_uid[g.ical_uid] = g

        links = self.store.all_links()
        linked = self._auto_link_existing(apple_by_uid, google_for_match, links)
        log_info("기존 일정 자동 매칭 %d쌍 (DB 링크 %d개)", linked, len(self.store.all_links()))
        links = self.store.all_links()

        self._apply_google_changes(
            google_delta,
            apple_by_uid,
            links,
            skip_apple_writes=skip_apple_to_google,
        )
        links = self.store.all_links()
        if not skip_apple_to_google:
            self._apply_apple_changes(
                list(apple_by_uid.values()),
                google_by_id,
                google_by_ical_uid,
                google_for_match,
                links,
            )
            self._reconcile_apple_deletions(
                apple_by_uid, self.store.all_links(), google_by_id
            )
        else:
            log_info(
                "이번 사이클은 iCal 기준선 — Apple→Google·Google→Apple 쓰기 생략 (링크만 저장)"
            )

    def _match_tolerance(self) -> int:
        if not self.cfg.get("fuzzy_match_enabled", True):
            return 0
        return int(self.cfg.get("fuzzy_match_tolerance_minutes", 5))

    def _link_grace_active(self, link: LinkRecord) -> bool:
        if link.last_origin != ORIGIN_GOOGLE:
            return False
        try:
            updated = datetime.fromisoformat(link.updated_at)
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated < ICAL_PUBLISH_GRACE

    def _save_link(
        self,
        apple_uid: str,
        gev: GoogleEvent,
        aev: AppleEvent,
        origin: str,
    ) -> None:
        self.store.upsert(
            apple_uid,
            gev.event_id,
            gev.ical_uid,
            aev.fingerprint(),
            gev.fingerprint(),
            origin,
        )

    def _replace_link_apple_uid(
        self,
        old_apple_uid: str,
        new_apple_uid: str,
        gev: GoogleEvent,
        aev: AppleEvent,
        origin: str,
    ) -> None:
        if old_apple_uid != new_apple_uid:
            self.store.delete(old_apple_uid)
        self._save_link(new_apple_uid, gev, aev, origin)

    def _apple_times_for_google(
        self, aev: AppleEvent
    ) -> tuple[datetime, datetime]:
        if aev.all_day:
            return normalize_all_day_bounds(aev.start, aev.end)
        return aev.start, aev.end

    def _ical_still_has_google_event(
        self,
        gev: GoogleEvent,
        apple_by_uid: Dict[str, AppleEvent],
    ) -> bool:
        """iCal 피드에 같은 일정(제목·날짜)이 남아 있으면 Google 취소만으로 Apple 삭제하지 않음."""
        tol = self._match_tolerance()
        matched = find_apple_for_google(gev, apple_by_uid, tol)
        return matched is not None

    def _resolve_link_for_ical_event(
        self,
        aev: AppleEvent,
        google_by_id: Dict[str, GoogleEvent],
    ) -> Optional[LinkRecord]:
        """DB apple_uid 가 iCal UID 와 다를 때( Google→Apple 직후 ) 기존 링크 찾기."""
        link = self.store.get_by_apple(aev.uid)
        if link:
            return link
        tol = self._match_tolerance()
        for old in self.store.all_links().values():
            gev = google_by_id.get(old.google_event_id)
            if not gev or gev.status == "cancelled":
                continue
            if events_match(aev, gev, tol):
                if old.apple_uid != aev.uid:
                    self._replace_link_apple_uid(
                        old.apple_uid, aev.uid, gev, aev, old.last_origin
                    )
                    log_info("링크 UID 정리 (iCal): %s", aev.summary)
                return self.store.get_by_apple(aev.uid)
        return None

    def _auto_link_existing(
        self,
        apple_by_uid: Dict[str, AppleEvent],
        google_events: List[GoogleEvent],
        links: Dict[str, LinkRecord],
    ) -> int:
        tol = self._match_tolerance()
        count = 0
        linked_google: set[str] = {r.google_event_id for r in links.values()}
        linked_apple: set[str] = set(links.keys())

        for gev in google_events:
            if gev.status == "cancelled":
                continue
            if gev.event_id in linked_google:
                continue
            if has_sync_marker(gev.description, self.marker):
                continue
            aev = find_apple_for_google(gev, apple_by_uid, tol)
            if not aev or aev.uid in linked_apple:
                continue
            self.store.upsert(
                aev.uid,
                gev.event_id,
                gev.ical_uid,
                aev.fingerprint(),
                gev.fingerprint(),
                ORIGIN_GOOGLE,
            )
            linked_apple.add(aev.uid)
            linked_google.add(gev.event_id)
            count += 1
            log_info("자동 매칭: %s", gev.summary)

        g_by_ical = {g.ical_uid: g for g in google_events if g.ical_uid}
        for aev in apple_by_uid.values():
            if aev.uid in linked_apple:
                continue
            gev = find_google_for_apple(aev, g_by_ical, google_events, tol)
            if not gev:
                continue
            if gev.event_id in linked_google:
                existing = self.store.get_by_google(gev.event_id)
                if existing and existing.apple_uid != aev.uid:
                    self.store.delete(existing.apple_uid)
                    linked_apple.discard(existing.apple_uid)
                    linked_google.discard(gev.event_id)
                else:
                    continue
            self._save_link(aev.uid, gev, aev, ORIGIN_APPLE)
            linked_apple.add(aev.uid)
            linked_google.add(gev.event_id)
            count += 1
            log_info("자동 매칭: %s", aev.summary)

        return count

    def _apply_google_changes(
        self,
        google_events: List[GoogleEvent],
        apple_by_uid: Dict[str, AppleEvent],
        links: Dict[str, LinkRecord],
        skip_apple_writes: bool = False,
    ) -> None:
        tol = self._match_tolerance()
        for gev in google_events:
            link = self.store.get_by_google(gev.event_id)
            if link and link.last_origin == ORIGIN_APPLE and gev.fingerprint() == link.google_fp:
                continue

            if gev.status == "cancelled":
                if link:
                    if self._ical_still_has_google_event(gev, apple_by_uid):
                        log_info(
                            "Google 취소 건너뜀 (Apple iCal에 일정 있음, Apple 삭제 안 함): %s",
                            gev.summary,
                        )
                    elif not skip_apple_writes:
                        log_info("Apple 삭제 시도 (Google 취소): %s", gev.summary)
                        apple.delete_event(
                            self.apple_cal, link.apple_uid, gev.start
                        )
                        log_info("Apple 삭제 완료 (Google 취소): %s", gev.summary)
                    else:
                        log_info(
                            "Google 취소 (DB 링크만 정리, Apple 삭제 생략): %s",
                            gev.summary,
                        )
                    self.store.delete(link.apple_uid)
                continue

            if link:
                aev = apple_by_uid.get(link.apple_uid)
                apple_fp = aev.fingerprint() if aev else link.apple_fp
                if (
                    aev
                    and aev.fingerprint() == link.apple_fp
                    and gev.fingerprint() == link.google_fp
                ):
                    continue
                if not core_fields_differ(aev, gev, tol) if aev else False:
                    self.store.upsert(
                        link.apple_uid,
                        gev.event_id,
                        gev.ical_uid,
                        apple_fp,
                        gev.fingerprint(),
                        ORIGIN_GOOGLE,
                    )
                    continue
                if skip_apple_writes:
                    self.store.upsert(
                        link.apple_uid,
                        gev.event_id,
                        gev.ical_uid,
                        apple_fp,
                        gev.fingerprint(),
                        ORIGIN_GOOGLE,
                    )
                    continue
                if not aev:
                    self.store.upsert(
                        link.apple_uid,
                        gev.event_id,
                        gev.ical_uid,
                        link.apple_fp,
                        gev.fingerprint(),
                        ORIGIN_GOOGLE,
                    )
                    continue
                log_info("Apple 수정 시도 (Google): %s", gev.summary)
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
                    log_info("Apple 생성 시도 (UID 없음, Google): %s", gev.summary)
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
                log_info("Apple 반영 완료 (Google): %s", gev.summary)
                self.store.upsert(
                    link.apple_uid,
                    gev.event_id,
                    gev.ical_uid,
                    aev.fingerprint(),
                    gev.fingerprint(),
                    ORIGIN_GOOGLE,
                )
                continue

            if has_sync_marker(gev.description, self.marker):
                continue

            matched = find_apple_for_google(gev, apple_by_uid, tol)
            if matched:
                existing = self.store.get_by_google(gev.event_id)
                if existing and existing.apple_uid != matched.uid:
                    self._replace_link_apple_uid(
                        existing.apple_uid,
                        matched.uid,
                        gev,
                        matched,
                        ORIGIN_GOOGLE,
                    )
                else:
                    self._save_link(matched.uid, gev, matched, ORIGIN_GOOGLE)
                log_info("매칭 연결만 (중복 생성 안 함): %s", gev.summary)
                continue

            if skip_apple_writes:
                continue

            apple_uid = gev.ical_uid or str(uuid.uuid4())
            if apple_uid in apple_by_uid:
                continue

            log_info("Apple 생성 시도 (Google): %s", gev.summary)
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
            log_info("Apple 생성 완료 (Google): %s", gev.summary)
            # iCal UID는 게시 URL에서 달라질 수 있음 — Google event id 로 연결
            fp = gev.fingerprint()
            self.store.upsert(
                apple_uid,
                gev.event_id,
                gev.ical_uid,
                fp,
                fp,
                ORIGIN_GOOGLE,
            )

    def _apply_apple_changes(
        self,
        apple_events: List[AppleEvent],
        google_by_id: Dict[str, GoogleEvent],
        google_by_ical_uid: Dict[str, GoogleEvent],
        google_events: List[GoogleEvent],
        links: Dict[str, LinkRecord],
    ) -> None:
        for aev in apple_events:
            link = self._resolve_link_for_ical_event(aev, google_by_id)
            if link and link.last_origin == ORIGIN_GOOGLE and aev.fingerprint() == link.apple_fp:
                continue

            desc = with_marker(aev.description, self.marker)
            g_start, g_end = self._apple_times_for_google(aev)

            if link:
                gev = google_by_id.get(link.google_event_id)
                if not gev:
                    continue
                # iCal이 마지막 동기화와 같으면 Google API 반올림 차이로 매 사이클 수정하지 않음
                if aev.fingerprint() == link.apple_fp:
                    if gev.fingerprint() != link.google_fp:
                        self.store.upsert(
                            aev.uid,
                            link.google_event_id,
                            gev.ical_uid or link.google_ical_uid,
                            link.apple_fp,
                            gev.fingerprint(),
                            ORIGIN_APPLE,
                        )
                    continue
                tol = self._match_tolerance()
                if not core_fields_differ(aev, gev, tol):
                    self.store.upsert(
                        aev.uid,
                        link.google_event_id,
                        link.google_ical_uid,
                        aev.fingerprint(),
                        gev.fingerprint(),
                        ORIGIN_APPLE,
                    )
                    continue
                log_info("Google 수정 시도 (Apple): %s", aev.summary)
                updated = google.update_event(
                    self.service,
                    self.google_cal,
                    link.google_event_id,
                    aev.summary,
                    desc,
                    aev.location,
                    g_start,
                    g_end,
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

            matched = find_google_for_apple(
                aev, google_by_ical_uid, google_events, self._match_tolerance()
            )
            if matched:
                existing = self.store.get_by_google(matched.event_id)
                if existing and existing.apple_uid != aev.uid:
                    self._replace_link_apple_uid(
                        existing.apple_uid, aev.uid, matched, aev, ORIGIN_APPLE
                    )
                    log_info("링크 UID 정리 (중복 생성 안 함): %s", aev.summary)
                else:
                    self._save_link(aev.uid, matched, aev, ORIGIN_APPLE)
                    log_info("매칭 연결만 (중복 생성 안 함): %s", aev.summary)
                continue

            marked_linked = False
            for g in google_events:
                if g.status == "cancelled":
                    continue
                if not has_sync_marker(g.description, self.marker):
                    continue
                if not events_match(aev, g, self._match_tolerance()):
                    continue
                existing = self.store.get_by_google(g.event_id)
                if existing and existing.apple_uid != aev.uid:
                    self._replace_link_apple_uid(
                        existing.apple_uid, aev.uid, g, aev, ORIGIN_APPLE
                    )
                else:
                    self._save_link(aev.uid, g, aev, ORIGIN_APPLE)
                log_info("동기화 표시 Google 일정 연결 (생성 안 함): %s", aev.summary)
                marked_linked = True
                break
            if marked_linked:
                continue

            ical_uid = aev.uid or str(uuid.uuid4())
            log_info("Google 생성 시도 (Apple): %s", aev.summary)
            try:
                created = google.create_event(
                    self.service,
                    self.google_cal,
                    ical_uid,
                    aev.summary,
                    desc,
                    aev.location,
                    g_start,
                    g_end,
                    aev.all_day,
                )
            except Exception:
                logging.exception("Google 생성 실패 (Apple): %s", aev.summary)
                continue
            log_info("Google 생성 완료 (Apple): %s", aev.summary)
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
        google_by_id: Dict[str, GoogleEvent],
    ) -> None:
        tol = self._match_tolerance()
        for apple_uid, link in list(links.items()):
            if apple_uid in apple_by_uid:
                continue
            gev = google_by_id.get(link.google_event_id)
            rematched = False
            if gev:
                for aev in apple_by_uid.values():
                    if events_match(aev, gev, tol):
                        self._replace_link_apple_uid(
                            apple_uid, aev.uid, gev, aev, link.last_origin
                        )
                        log_info(
                            "iCal UID 변경으로 링크 갱신 (Google 삭제 안 함): %s",
                            aev.summary,
                        )
                        rematched = True
                        break
            if rematched:
                continue
            if self._link_grace_active(link):
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
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="sync_state.db 삭제 후 종료 (중복 정리 후 1회 사용)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["log_path"])

    if args.reset_state:
        db = expand(cfg["state_db_path"])
        if db.exists():
            db.unlink()
            log_info("상태 DB 삭제: %s", db)
        else:
            log_info("상태 DB 없음: %s", db)
        return

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
