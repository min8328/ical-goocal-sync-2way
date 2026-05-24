import requests
import time
import json
import os
from icalendar import Calendar

# --- 설정 구간 ---
# 1단계에서 복사한 주소 중 'webcal://'를 'https://'로 바꿔서 입력하세요.
ICAL_URL = "https://p150-caldav.icloud.com/published/2/NDA3NDU2MjAyNDA3NDU2MhQJ3JpaauxCq5FYNGljawRFAMHc0NP3TeQTHvAN0o-5Wz9q1aBl3EnBTbT9_w8FEUdnTqIfw_wP_ZC7lpM2oxM" 
PUSHOVER_TOKEN = "av86rffrm94nwwomo8t2o4y86vxee4"
PUSHOVER_USER = "u1b86q7yhtqaq9cgj7u84ocytqnuse"
DB_FILE = os.path.expanduser("~/Documents/calendar_state.json")
CHECK_INTERVAL = 1  # 체크 주기 (초 단위, 180초 = 3분)
# ----------------

def get_calendar_events():
    try:
        response = requests.get(ICAL_URL)
        if response.status_code != 200:
            return None
        
        cal = Calendar.from_ical(response.content)
        events = {}
        
        for component in cal.walk():
            if component.name == "VEVENT":
                # 일정의 고유 ID(UID)를 키로 사용
                uid = str(component.get('uid'))
                events[uid] = {
                    "summary": str(component.get('summary', '제목 없음')),
                    "start": str(component.get('dtstart').dt),
                    "last_modified": str(component.get('last-modified').dt) if component.get('last-modified') else "N/A"
                }
        return events
    except Exception as e:
        print(f"에러 발생: {e}")
        return None

def send_pushover(title, message):
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message
    }
    requests.post("https://api.pushover.net/1/messages.json", data=data)

def load_previous_state():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_current_state(state):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=4)

print("iCloud 가족 캘린더 모니터링 시작...")

# 첫 실행 시 현재 상태 저장 (최초 실행 시 대량 알림 방지)
current_state = get_calendar_events()
if current_state:
    save_current_state(current_state)

while True:
    time.sleep(CHECK_INTERVAL)
    
    old_state = load_previous_state()
    new_state = get_calendar_events()
    
    if not new_state:
        continue
        
    # 1. 신규 추가 및 수정 확인
    for uid, event in new_state.items():
        if uid not in old_state:
            # 신규 일정 추가됨
            msg = f"새 일정: {event['summary']}\n일시: {event['start']}"
            send_pushover("📅 가족 캘린더 추가", msg)
        elif old_state[uid]['last_modified'] != event['last_modified']:
            # 기존 일정이 수정됨
            msg = f"수정됨: {event['summary']}\n변경 일시: {event['start']}"
            send_pushover("✏️ 가족 캘린더 수정", msg)
            
    # 2. 삭제 확인
    for uid, event in old_state.items():
        if uid not in new_state:
            # 일정이 삭제됨
            msg = f"삭제된 일정: {event['summary']}\n원래 일시: {event['start']}"
            send_pushover("❌ 가족 캘린더 삭제", msg)
            
    save_current_state(new_state)
