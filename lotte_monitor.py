#!/usr/bin/env python3
"""
롯데시네마 무대인사/GV/시사회 모니터링 스크립트
새로운 이벤트 상영이 등록되면 Discord로 알림을 보냅니다.
"""

import requests
import json
import os
import random
from datetime import datetime, timezone, timedelta
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 설정
# 웹훅은 시크릿/환경변수로만 받는다 (LOTTE_DISCORD_WEBHOOK_URL -> DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lotte_events.json")

# 롯데시네마 API URLs
CINEMA_URL = "https://www.lottecinema.co.kr/LCWS/Cinema/CinemaData.aspx"
TICKETING_URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.lottecinema.co.kr/NLCHS/Ticketing"
}

# 이벤트 타입 코드 (일반=10 제외)
EVENT_CODES = {
    30: "무대인사",
    40: "GV",
    50: "시사회",
    230: "스페셜상영회",
}

# 서울/경기 지역 영화관 (알림 대상)
SEOUL_GYEONGGI_CINEMAS = [
    # 서울
    "가산디지털", "가양", "강동", "건대입구", "김포공항", "노원", "도곡", "독산",
    "서울대입구", "수락산", "신도림", "신림", "에비뉴엘", "영등포", "용산", "월드타워",
    "은평", "청량리", "합정", "홍대입구", "중랑", "천호", "신대방", "구로",
    # 경기
    "광명", "광명아울렛", "구리", "동탄", "라페스타", "마석", "부천", "부천역",
    "분당", "산본", "성남", "수원", "시화", "안산", "안성", "안양", "안양일번가",
    "야탑", "오산", "용인", "의정부", "의정부민락", "일산", "죽전", "판교",
    "파주아울렛", "평택", "평촌", "하남미사", "화정", "수지", "동수원", "광교",
    "인덕원", "범계", "기흥", "김포", "고양스타필드", "위례", "동탄역",
]


def load_saved_events():
    """저장된 이벤트 목록 불러오기"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_events(events):
    """이벤트 목록 저장"""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def get_all_cinemas():
    """서울/경기 영화관 목록 가져오기"""
    data = {
        "paramList": json.dumps({
            "MethodName": "GetCinemaItems",
            "channelType": "HO",
            "osType": "Chrome",
            "osVersion": "Mozilla/5.0"
        })
    }

    try:
        response = requests.post(CINEMA_URL, headers=HEADERS, data=data, timeout=10)
        result = response.json()

        if result.get("IsOK") == "true":
            cinemas = result.get("Cinemas", {}).get("Items", [])
            # 국내 영화관 중 서울/경기만 필터링
            return [c for c in cinemas
                    if c.get("DivisionCode") == 1
                    and c.get("CinemaNameKR") in SEOUL_GYEONGGI_CINEMAS]
    except Exception as e:
        print(f"[{datetime.now()}] 영화관 목록 조회 실패: {e}")

    return []


def fetch_cinema_events(cinema, dates):
    """단일 영화관의 이벤트 조회"""
    events = {}
    cinema_id = f"1|0001|{cinema['CinemaID']}"
    cinema_name = cinema['CinemaNameKR']

    for date in dates:
        try:
            data = {
                "paramList": json.dumps({
                    "MethodName": "GetPlaySequence",
                    "channelType": "HO",
                    "osType": "Chrome",
                    "osVersion": "Mozilla/5.0",
                    "playDate": date,
                    "cinemaID": cinema_id,
                    "representationMovieCode": ""
                })
            }

            response = requests.post(TICKETING_URL, headers=HEADERS, data=data, timeout=10)
            result = response.json()

            for item in result.get("PlaySeqs", {}).get("Items", []):
                accompany_code = item.get("AccompanyTypeCode")
                accompany_name = item.get("AccompanyTypeNameKR", "")

                # 이벤트 코드이거나 이벤트 키워드 포함
                is_event = (
                    accompany_code in EVENT_CODES or
                    "무대인사" in accompany_name or
                    "GV" in accompany_name or
                    "시사회" in accompany_name or
                    "스페셜" in accompany_name
                )

                if is_event:
                    # 고유 ID 생성
                    event_id = f"{cinema['CinemaID']}_{date}_{item.get('StartTime')}_{item.get('MovieCode')}"

                    if event_id not in events:
                        events[event_id] = {
                            "id": event_id,
                            "cinemaID": cinema['CinemaID'],
                            "cinemaName": cinema_name,
                            "movieCode": item.get("MovieCode"),
                            "movieName": item.get("MovieNameKR"),
                            "playDate": date,
                            "startTime": item.get("StartTime"),
                            "endTime": item.get("EndTime"),
                            "screenName": item.get("ScreenNameKR"),
                            "eventType": accompany_name or EVENT_CODES.get(accompany_code, "특별상영"),
                            "eventCode": accompany_code,
                            "totalSeat": item.get("TotalSeatCount", 0),
                            "restSeat": item.get("RemainSeatCount", 0),
                        }
        except:
            continue

    return events


def fetch_events(cinemas, days=7):
    """이벤트 상영 조회 (병렬 처리)"""
    events = {}
    dates = [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    print(f"[{datetime.now()}] 병렬 조회 시작 ({len(cinemas)}개 영화관, {days}일)...")

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_cinema_events, c, dates): c for c in cinemas}

        completed = 0
        for future in as_completed(futures):
            cinema_events = future.result()
            events.update(cinema_events)
            completed += 1

            if completed % 30 == 0:
                print(f"[{datetime.now()}] 진행: {completed}/{len(cinemas)} 영화관, 발견: {len(events)}개")

    return events


def send_discord_notification(event):
    """Discord로 알림 보내기"""
    if not DISCORD_WEBHOOK_URL:
        print(f"[{datetime.now()}] Discord webhook URL이 설정되지 않았습니다.")
        return False

    # 날짜 포맷팅
    play_date = event["playDate"]
    formatted_date = play_date  # 이미 YYYY-MM-DD 형식

    # 예매 URL
    booking_url = f"https://www.lottecinema.co.kr/NLCHS/Ticketing"

    embed = {
        "embeds": [
            {
                "title": f"🎬 [{event['eventType']}] 롯데시네마",
                "description": event["movieName"],
                "url": booking_url,
                "color": 0xFFFFFF,  # 흰색
                "fields": [
                    {"name": "📍 지점", "value": event["cinemaName"], "inline": True},
                    {"name": "📅 날짜", "value": formatted_date, "inline": True},
                    {"name": "⏰ 시간", "value": f"{event['startTime']} ~ {event['endTime']}", "inline": True},
                    {"name": "🎥 상영관", "value": event["screenName"] or "-", "inline": True},
                ],
                "footer": {"text": "롯데시네마 이벤트 모니터"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            print(f"[{datetime.now()}] 알림 전송 완료: {event['movieName']} @ {event['cinemaName']}")
            return True
        else:
            print(f"[{datetime.now()}] 알림 전송 실패: {response.status_code}")
            return False
    except Exception as e:
        print(f"[{datetime.now()}] Discord 전송 오류: {e}")
        return False


def main():
    # 랜덤 딜레이 (0~30초) - 봇 패턴 회피
    delay = random.randint(0, 30)
    print(f"[{datetime.now()}] 랜덤 딜레이: {delay}초")
    time.sleep(delay)

    print(f"[{datetime.now()}] 롯데시네마 이벤트 모니터링 시작...")
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL이 비어 있습니다 - 알림이 발송되지 않습니다. "
              "LOTTE_DISCORD_WEBHOOK_URL 시크릿을 확인하세요")
    start_time = time.time()

    # 저장된 이벤트 불러오기
    saved_events = load_saved_events()
    is_first_run = len(saved_events) == 0

    if is_first_run:
        print(f"[{datetime.now()}] 첫 실행 - 기존 이벤트 수집 중...")

    # 전체 영화관 목록 가져오기
    print(f"[{datetime.now()}] 영화관 목록 조회 중...")
    cinemas = get_all_cinemas()
    print(f"[{datetime.now()}] 전체 영화관 수: {len(cinemas)}")

    if not cinemas:
        print(f"[{datetime.now()}] 영화관 목록을 가져올 수 없습니다.")
        return

    # 이벤트 상영 조회 (7일)
    print(f"[{datetime.now()}] 이벤트 상영 조회 중 (14일간)...")
    current_events = fetch_events(cinemas, days=14)
    print(f"[{datetime.now()}] 발견된 이벤트: {len(current_events)}개")

    # 새로운 이벤트 찾기
    new_events = []
    for event_id, event in current_events.items():
        if event_id not in saved_events:
            new_events.append(event)

    print(f"[{datetime.now()}] 새로운 이벤트: {len(new_events)}개")

    # 새 이벤트 알림 보내기 (서울/경기 지역만)
    if not is_first_run and new_events:
        for event in new_events:
            if event.get("cinemaName") in SEOUL_GYEONGGI_CINEMAS:
                send_discord_notification(event)
                time.sleep(0.5)  # Discord rate limit 방지

    # 이벤트 저장 (기존 + 새로운)
    saved_events.update(current_events)

    # 오래된 이벤트 정리 (14일 이상 지난 이벤트 삭제)
    cutoff_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    saved_events = {k: v for k, v in saved_events.items() if v.get("playDate", "9999-99-99") >= cutoff_date}

    save_events(saved_events)

    elapsed = time.time() - start_time
    print(f"[{datetime.now()}] 완료! 소요 시간: {elapsed:.1f}초")

    if is_first_run:
        print(f"[{datetime.now()}] 첫 실행 완료 - {len(current_events)}개 이벤트 저장됨")
        if DISCORD_WEBHOOK_URL:
            test_msg = {
                "content": f"✅ 롯데시네마 이벤트 모니터링이 시작되었습니다!\n현재 {len(current_events)}개의 이벤트 상영을 추적 중입니다."
            }
            try:
                requests.post(DISCORD_WEBHOOK_URL, json=test_msg, timeout=10)
            except:
                pass


if __name__ == "__main__":
    main()
