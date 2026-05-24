# Apple 가족 캘린더 ↔ Google Calendar 동기화 데몬

맥북(MacOS 10.14+)에서 **Apple 가족 캘린더** 와 **Google Calendar** 를 약 1분 주기로 양방향 동기화합니다.

- 아내가 iPhone에서 가족 캘린더 수정 → 맥이 Google에 반영 → **갤럭시 Google Calendar 알림**
- 본인이 갤럭시에서 Google 수정 → 맥이 Apple `가족` 캘린더에 반영 → **iPhone Apple 알림**

## 권장 방식: `published_ical` (기본)

`ical_monitor.py` 와 같이 **iCloud 게시(공개) ICS URL** 로 Apple 쪽 **읽기·변경 감지**를 합니다.

- Calendar.app AppleScript 불필요 → Mojave `-1712` / 구문 오류 회피
- **Google → Apple** 쓰기만 Calendar.app AppleScript 사용 (`apple_calendar_name`)

## 요구 사항

- macOS 10.14+, Python 3.10
- iCloud **가족** 캘린더 **게시 URL** (`webcal` → `https`)
- Google OAuth (`credentials.json`, `token.json`)
- Google → Apple 반영 시: Calendar.app 권한·`가족` 캘린더 이름

## 설치

```bash
mkdir -p ~/calendar-sync
cd ~/calendar-sync
# 저장소 apple-google-calendar-sync/* 복사
python3.10 -m pip install -r requirements.txt
cp config.example.yaml config.yaml
```

### 게시 URL 받기

1. Mac **캘린더** 앱 → 왼쪽 **가족** 캘린더
2. **게시…** (또는 공유/게시) → **공개 캘린더 게시** 켜기
3. `webcal://p…-caldav.icloud.com/published/…` 복사
4. `config.yaml` 의 `apple_published_url` 에 넣을 때 `webcal://` → `https://` 로 변경

```yaml
apple_source: "published_ical"
apple_published_url: "https://pXXX-caldav.icloud.com/published/..."
apple_calendar_name: "가족"
google_calendar_id: "primary"
```

### Google OAuth

```bash
python3.10 sync_daemon.py --auth
```

### 테스트 순서

```bash
# 1) iCal 읽기만
python3.10 -u sync_daemon.py --apple-only

# 2) 첫 실행 — 기준선만 (대량 Google 생성 방지, 권장)
python3.10 -u sync_daemon.py --once

# 3) 가족 캘린더에 테스트 일정 추가 → 1~2분 후 Google primary 확인

# (선택) 기존 일정 전부 Google로 옮기기
python3.10 -u sync_daemon.py --once --bootstrap
```

### Google → Apple 권한

**시스템 환경설정 → 보안 및 개인 정보 보호 → 캘린더·자동화** 에서 Python/터미널 허용.

## `calendar_app` 모드 (비권장)

Mojave에서 AppleScript 타임아웃이 잦으면 다음으로 전환:

```yaml
apple_source: "calendar_app"
```

## launchd

`com.calendar.apple-google-sync.plist` 경로 수정 후:

```bash
cp com.calendar.apple-google-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.calendar.apple-google-sync.plist
tail -f ~/calendar-sync/sync.log
```

## 동작 요약

| 방향 | published_ical 모드 |
|------|---------------------|
| Apple → Google | ICS URL 폴링 + UID/LAST-MODIFIED |
| Google → Apple | Calendar.app AppleScript |
| 상태 | `sync_state.db` |

## 예제 스크립트

| 파일 | 용도 |
|------|------|
| `ical_monitor.py` | ICS 변경 → Pushover (참고용) |
| `cal_sync_two_way.py` | 구버전 프로토타입 (중복 생성 위험) |

## 문제 해결

| 증상 | 조치 |
|------|------|
| `apple_published_url` 오류 | URL·https·게시 켜기 확인 |
| 첫 `--once` 후 Google에 안 생김 | 정상(기준선). 일정 추가 후 다음 주기 확인 |
| Google→Apple 실패 | `apple_calendar_name`, 캘린더 권한 |
| `-1712` | `apple_source: published_ical` 사용 |

## 보안

게시 URL·Pushover 토큰은 **비밀번호와 같음**. Git에 올리지 마세요.
