import os
import sys
import datetime
import json
import subprocess
import time
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests
from icalendar import Calendar

# --- 고정 경로 설정 ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = os.path.expanduser('~/Documents/credentials.json') 
TOKEN_FILE = os.path.expanduser('~/Documents/token.json')
CONFIG_FILE = os.path.expanduser('~/Documents/config.json')

def load_config():
    default_config = {
        "ICLOUD_URL": "",
        "GOOGLE_CALENDAR_ID": "primary",
        "ICLOUD_CALENDAR_NAME": "가족"
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default_config

def check_and_refresh_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            return None, False

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
            print("🔄 구글 인증 토큰이 안전하게 갱신되었습니다.")
            return creds, True
        except Exception as e:
            print(f"⚠️ 토큰 자동 갱신 보류: {e}")
            return None, False
    return creds, False

def get_icloud_events(icloud_url):
    if not icloud_url:
        return {}
    try:
        response = requests.get(icloud_url, timeout=15) 
        if response.status_code != 200:
            return {}
        cal = Calendar.from_ical(response.content)
        events = {}
        for component in cal.walk():
            if component.name == "VEVENT":
                uid = str(component.get('uid'))
                summary = str(component.get('summary', '제목 없음'))
                
                events[uid] = {
                    "summary": summary,
                    "start": component.get('dtstart').dt,
                    "end": component.get('dtend').dt if component.get('dtend') else component.get('dtstart').dt,
                }
        return events
    except Exception as e:
        print(f"iCloud 다운로드 실패: {e}")
        return {}

def sync_calendars(service):
    config = load_config()
    icloud_events = get_icloud_events(config["ICLOUD_URL"])
    if not icloud_events:
        return
        
    now_dt = datetime.datetime.now()
    now_date = now_dt.date()
    
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    time_min = now_utc.isoformat().replace('+00:00', 'Z')
    time_max = (now_utc + datetime.timedelta(days=365)).isoformat().replace('+00:00', 'Z')
    
    google_result = service.events().list(
        calendarId=config["GOOGLE_CALENDAR_ID"], 
        timeMin=time_min, 
        timeMax=time_max, 
        maxResults=250, 
        singleEvents=True
    ).execute()

    raw_google_events = google_result.get('items', [])
    
    # 💡 [핵심 구현] 복잡한 제목 검사 제거! 오직 구글 시스템 자동 생성(생일/휴일 등) 계정만 완벽히 걸러냅니다.
    google_events = []
    for g in raw_google_events:
        org_email = g.get('organizer', {}).get('email', '')
        if 'group.v.calendar.google.com' in org_email or org_email == 'unknownorganizer@calendar.google.com':
            continue
        google_events.append(g)

    # 고유 ID 매핑 테이블 빌드 (이미 동기화된 녀석들 구별용)
    g_uids_map = {}       
    for g in google_events:
        if 'extendedProperties' in g and 'private' in g['extendedProperties'] and 'iCloudUID' in g['extendedProperties']['private']:
            uid = g['extendedProperties']['private']['iCloudUID']
            g_uids_map[uid] = g
    
    # 1. iCloud ➔ 구글 동기화
    for uid, i_event in icloud_events.items():
        # 💡 과도한 제목/날짜 중복 필터링을 제거하고 고유 ID 존재 여부만 정직하게 체크합니다.
        if uid in g_uids_map:
            continue

        is_datetime = isinstance(i_event['start'], datetime.datetime)
        start_str = i_event['start'].isoformat() if is_datetime else i_event['start'].strftime('%Y-%m-%d')
        print_date = start_str[:10]

        if is_datetime:
            event_date = i_event['start'].replace(tzinfo=None)
            end_str = i_event['end'].isoformat()
            time_key = 'dateTime'
            if event_date < now_dt or event_date > (now_dt + datetime.timedelta(days=365)):
                continue
        else:
            event_date = i_event['start']
            end_date = i_event['end'] if isinstance(i_event['end'], datetime.date) else i_event['start']
            if isinstance(end_date, datetime.datetime):
                end_str = (end_date.date() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            else:
                end_str = (end_date + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                
            time_key = 'date'
            if event_date < now_date or event_date > (now_date + datetime.timedelta(days=365)):
                continue
            
        event_body = {
            'summary': i_event['summary'],
            'start': {time_key: start_str},
            'end': {time_key: end_str},
            'extendedProperties': {'private': {'iCloudUID': uid}}
        }
        
        if is_datetime:
            event_body['start']['timeZone'] = 'Asia/Seoul'
            event_body['end']['timeZone'] = 'Asia/Seoul'
        
        try:
            service.events().insert(calendarId=config["GOOGLE_CALENDAR_ID"], body=event_body).execute()
            print(f"[{print_date}] 구글에 일정 동기화 완료: {i_event['summary']}")
        except Exception as ex:
            print(f"구글 주입 실패: {ex}")

    # 2. 갤럭시(구글) ➔ iCloud 동기화
    if sys.platform != 'darwin':
        return

    for g_event in google_events:
        # 💡 이미 iCloud에서 넘어온 일정이 아니라면 무조건 정직하게 주입합니다.
        is_from_icloud = False
        if 'extendedProperties' in g_event and 'private' in g_event['extendedProperties']:
            if 'iCloudUID' in g_event['extendedProperties']['private']:
                is_from_icloud = True
                
        if is_from_icloud:
            continue
            
        g_summary = g_event.get('summary', '').strip()
        g_summary_escaped = g_summary.replace('"', '\\"')
        g_start = g_event['start'].get('dateTime', g_event['start'].get('date'))[:10]
        
        if 'dateTime' in g_event['start']:
            g_start_raw = g_event['start']['dateTime']
            script_date_str = f'date "{g_start_raw}"'
        else:
            g_start_raw = g_event['start']['date']
            script_date_str = f'date "{g_start_raw} 00:00:00"'
        
        apple_script = f'''
        tell application "Calendar"
            tell calendar "{config["ICLOUD_CALENDAR_NAME"]}"
                make new event with properties {{summary:"{g_summary_escaped}", start date:{script_date_str}}}
            end tell
        end tell
        '''
        try:
            subprocess.run(['osascript', '-e', apple_script], capture_output=True, text=True, check=True)
            print(f"[{g_start}] iCloud 가족 캘린더에 갤럭시 일정 주입 완료: {g_summary}")
        except Exception as ex:
            print(f"iCloud 주입 오류: {ex}")

if __name__ == '__main__':
    print("🚀 구글 시스템 생일 필터가 탑재된 양방향 동기화 엔진 구동...")
    
    current_creds, _ = check_and_refresh_credentials()
    if not current_creds:
        if os.path.exists(TOKEN_FILE):
            current_creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            current_creds = flow.run_local_server(host='127.0.0.1', port=0, open_browser=False)
            with open(TOKEN_FILE, 'w') as token:
                token.write(current_creds.to_json())

    global_service = build('calendar', 'v3', credentials=current_creds, cache_discovery=False)
    LOOP_INTERVAL = 60 

    while True:
        try:
            updated_creds, is_refreshed = check_and_refresh_credentials()
            if is_refreshed and updated_creds:
                global_service = build('calendar', 'v3', credentials=updated_creds, cache_discovery=False)
            sync_calendars(global_service)
            time.sleep(LOOP_INTERVAL)
        except KeyboardInterrupt:
            print("\n🛑 데몬을 종료합니다.")
            sys.exit(0)
        except Exception as e:
            print(f"🔄 대기 후 재시도: {e}")
            time.sleep(30)