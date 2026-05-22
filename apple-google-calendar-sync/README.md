# Apple 가족 캘린더 ↔ Google Calendar 동기화 데몬

맥북(MacOS 10.14+)에서 **Calendar.app의 공유 캘린더(예: `가족`)** 와 **Google Calendar** 를 약 1분 주기로 양방향 동기화합니다.

- 아내가 iPhone에서 Apple 가족 캘린더를 수정 → 맥이 Google에 반영 → 갤럭시에서 Google 알림
- 본인이 갤럭시에서 Google 캘린더를 수정 → 맥이 Apple에 반영 → iPhone에서 Apple 알림

서드파티 캘린더 앱 없이, **기본 Calendar + Google Calendar** 만 사용합니다.

## 요구 사항

- macOS 10.14 (Mojave) 이상
- Python 3.10
- 맥에 로그인된 iCloud 계정 + Calendar.app에 `가족` 캘린더 표시
- Google Cloud OAuth 클라이언트 (`credentials.json`, `token.json`)
- Amphetamine 등으로 맥 상시 깨우기 (권장)

## 설치 (맥북에서)

### 1. 폴더 복사

```bash
mkdir -p ~/calendar-sync
# 이 저장소의 apple-google-calendar-sync/* 파일을 ~/calendar-sync/ 로 복사
cd ~/calendar-sync
```

### 2. Python 패키지

```bash
python3.10 -m pip install -r requirements.txt
```

### 3. 설정 파일

```bash
cp config.example.yaml config.yaml
# config.yaml 에서 경로·캘린더 이름 수정
```

| 항목 | 설명 |
|------|------|
| `apple_calendar_name` | Calendar.app에 보이는 정확한 이름 (예: `가족`) |
| `google_calendar_id` | `primary` 또는 Google 설정의 캘린더 ID |
| `poll_interval_seconds` | `60` 권장 (1분) |

Google OAuth 파일은 보통 `~/calendar-sync/credentials.json`, `~/calendar-sync/token.json` 에 둡니다.

### 4. Google OAuth (최초 1회)

맥에서 브라우저로 로그인해야 합니다.

```bash
cd ~/calendar-sync
python3.10 sync_daemon.py --auth
```

### 5. macOS 권한

**시스템 환경설정 → 보안 및 개인 정보 보호 → 개인 정보 보호**

- **캘린더**: 터미널(또는 launchd가 사용하는 Python) 허용
- **자동화**(있는 경우): Python/터미널이 **Calendar** 제어 허용

권한 없으면 AppleScript가 실패합니다.

### 6. 수동 테스트

```bash
python3.10 sync_daemon.py --once
tail -f ~/calendar-sync/sync.log
```

Apple·Google 양쪽에 테스트 일정을 하나 넣고, 약 1분 내 반대편에 생기는지 확인하세요.

## launchd로 상시 실행

### 1. plist 수정

`com.calendar.apple-google-sync.plist` 에서 다음을 본인 환경에 맞게 바꿉니다.

- `YOUR_USER` → 실제 macOS 사용자명
- `python3.10` 경로 (`which python3.10` 결과)

### 2. 등록

```bash
cp com.calendar.apple-google-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.calendar.apple-google-sync.plist
```

### 3. 상태 확인

```bash
launchctl list | grep apple-google-sync
tail -f ~/calendar-sync/sync.log
```

### 중지/재시작

```bash
launchctl unload ~/Library/LaunchAgents/com.calendar.apple-google-sync.plist
launchctl load ~/Library/LaunchAgents/com.calendar.apple-google-sync.plist
```

## 동작 방식

1. **Apple**: 지정 기간 내 일정을 AppleScript로 전체 조회
2. **Google**: `syncToken` 으로 변경분만 조회 (삭제는 `cancelled` 상태로 수신)
3. **SQLite** (`sync_state.db`): Apple UID ↔ Google event ID 매핑, fingerprint로 루프 방지
4. 변경이 있으면 반대쪽에 생성/수정/삭제

### 알림 팁

- **갤럭시**: Google Calendar 앱에서 해당 캘린더 알림 켜기
- **iPhone**: Apple 캘린더 `가족` 알림 켜기 (기존과 동일)

맥이 꺼지거나 iCloud/Google 동기화가 지연되면 1분보다 늦을 수 있습니다.

## 문제 해결

| 증상 | 확인 |
|------|------|
| `Calendar not found` | `apple_calendar_name` 이 Calendar.app 이름과 일치하는지 |
| AppleScript 오류 | 캘린더·자동화 권한, Calendar.app 실행 여부 |
| Google 401/403 | `python3.10 sync_daemon.py --auth` 재실행, API에서 Calendar API 활성화 |
| 같은 일정이 반복 생성 | `sync_state.db` 백업 후 삭제하고 `--once` 로 재시작 (초기 1회만) |
| 삭제가 안 맞음 | 한쪽에서 삭제 후 1~2분 대기; 로그에 `삭제` 메시지 확인 |

## 파일 구성

```
sync_daemon.py      # 메인 데몬
apple_calendar.py   # Calendar.app (AppleScript)
google_calendar.py  # Google Calendar API
sync_store.py       # SQLite 상태
config.yaml         # 설정 (직접 생성)
sync_state.db       # 자동 생성
```

## 주의

- **반복 일정**은 iCloud/AppleScript 제약으로 동작이 불완전할 수 있습니다.
- **최초 실행** 시 기존 일정이 많으면 양쪽에 매핑되며, 테스트 후 운영하는 것을 권장합니다.
- 맥이 잠자기에 들어가면 동기화가 멈춥니다. Amphetamine·전원 연결·`caffeinate` 등을 사용하세요.
