#!/usr/bin/env python3
"""
메가박스 무대인사/GV/시사회 모니터링 스크립트
새로운 이벤트 상영이 등록되면 Discord로 알림을 보냅니다.
"""

import requests
import json
import os
import random
import html
from datetime import datetime, timezone, timedelta
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 설정
# Discord Webhook URL (환경변수 또는 기본값)
# 웹훅은 시크릿/환경변수로만 받는다 (MEGABOX_DISCORD_WEBHOOK_URL -> DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "megabox_events.json")

# 알림 대상 지역 (서울/경기만)
TARGET_REGIONS = ["서울", "경기"]

# 이벤트 키워드
EVENT_KEYWORDS = [
    "무대인사", "GV", "관객과의대화", "관객과의 대화",
    "시사회", "라이브뷰잉", "라이브 뷰잉", "LIVE", "Live",
    "콘서트", "concert", "싱어롱", "sing-along", "응원상영",
    "큐앤에이", "Q&A", "토크", "굿즈", "특별상영"
]

# 메가박스 API 설정
MEGABOX_API_URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"
MEGABOX_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.megabox.co.kr/booking"
}


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


def is_event_show(movie_name, event_div_cd=None, ctts_ty_div_cd=None):
    """이벤트 상영인지 확인"""
    # 이벤트 코드가 있으면 이벤트 상영
    if event_div_cd:
        return True

    # 영화 제목에 이벤트 키워드가 있으면 이벤트 상영
    movie_name_lower = movie_name.lower()
    for keyword in EVENT_KEYWORDS:
        if keyword.lower() in movie_name_lower:
            return True

    return False


def get_all_branches():
    """서울/경기 지점 목록 가져오기"""
    data = {
        "arrMovieNo": "",
        "playDe": datetime.now().strftime("%Y%m%d"),
        "brchNoListCnt": 1,
        "brchNo1": "1351",  # 코엑스 (기준점)
        "areaCd1": "",
        "theabKindCd1": "",
        "movieNo1": "",
        "sellChnlCd": ""
    }

    try:
        response = requests.post(MEGABOX_API_URL, headers=MEGABOX_HEADERS, json=data, timeout=10)
        result = response.json()

        branches = []
        for area in result.get("areaBrchList", []):
            # 서울/경기 지역만 필터링
            if area.get("areaCdNm") in TARGET_REGIONS:
                branches.append({
                    "brchNo": area.get("brchNo"),
                    "brchNm": area.get("brchNm"),
                    "areaCdNm": area.get("areaCdNm")
                })

        return branches
    except Exception as e:
        print(f"[{datetime.now()}] 지점 목록 조회 실패: {e}")
        return []


def fetch_branch_events(brch, dates):
    """단일 지점의 이벤트 조회 (병렬 처리용)"""
    branch_events = {}
    brch_no = brch["brchNo"]
    brch_nm = brch["brchNm"]

    for date in dates:
        data = {
            "arrMovieNo": "",
            "playDe": date,
            "brchNoListCnt": 1,
            "brchNo1": brch_no,
            "areaCd1": "",
            "theabKindCd1": "",
            "movieNo1": "",
            "sellChnlCd": ""
        }

        try:
            response = requests.post(MEGABOX_API_URL, headers=MEGABOX_HEADERS, json=data, timeout=10)
            result = response.json()

            for show in result.get("movieFormList", []):
                # 메가박스 API가 &#40; 같은 HTML 숫자 엔티티로 텍스트를 내려준다.
                # Discord는 이를 해석하지 않고 그대로 보여주므로 여기서 디코딩해둔다.
                movie_nm = html.unescape(show.get("movieNm", ""))
                event_div_cd = show.get("eventDivCd")
                ctts_ty_div_cd = show.get("cttsTyDivCd")

                if is_event_show(movie_nm, event_div_cd, ctts_ty_div_cd):
                    play_schdl_no = show.get("playSchdlNo", "")
                    event_id = f"{brch_no}_{date}_{show.get('playStartTime', '')}_{show.get('movieNo', '')}"

                    if event_id not in branch_events:
                        matched_keywords = [kw for kw in EVENT_KEYWORDS if kw.lower() in movie_nm.lower()]

                        branch_events[event_id] = {
                            "id": event_id,
                            "playSchdlNo": play_schdl_no,
                            "movieNo": show.get("movieNo", ""),
                            "movieNm": movie_nm,
                            "brchNo": brch_no,
                            "brchNm": html.unescape(brch_nm),
                            "areaCdNm": html.unescape(brch.get("areaCdNm", "")),
                            "playDe": date,
                            "playStartTime": show.get("playStartTime", ""),
                            "playEndTime": show.get("playEndTime", ""),
                            "theabExpoNm": html.unescape(show.get("theabExpoNm", "")),
                            "eventDivCdNm": html.unescape(show.get("eventDivCdNm", "")),
                            "restSeatCnt": show.get("restSeatCnt", 0),
                            "totSeatCnt": show.get("totSeatCnt", 0),
                            "bokdAbleAt": show.get("bokdAbleAt", "N"),
                            "matchedKeywords": matched_keywords,
                            "moviePosterImg": show.get("moviePosterImg", "")
                        }
        except:
            continue

    return branch_events


def fetch_events(branches, days=7):
    """이벤트 상영 조회 (병렬 처리)"""
    events = {}
    dates = [(datetime.now() + timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

    print(f"[{datetime.now()}] 병렬 조회 시작 ({len(branches)}개 지점, {days}일)...")

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_branch_events, brch, dates): brch for brch in branches}

        completed = 0
        for future in as_completed(futures):
            branch_events = future.result()
            events.update(branch_events)
            completed += 1

            if completed % 30 == 0:
                print(f"[{datetime.now()}] 진행: {completed}/{len(branches)} 지점, 발견: {len(events)}개")

    return events


def send_discord_notification(event):
    """Discord로 알림 보내기"""
    if not DISCORD_WEBHOOK_URL:
        print(f"[{datetime.now()}] Discord webhook URL이 설정되지 않았습니다.")
        return False

    # 날짜 포맷팅
    play_de = event["playDe"]
    formatted_date = f"{play_de[:4]}-{play_de[4:6]}-{play_de[6:]}"

    # 예매 가능 여부
    bokd_status = "예매 가능" if event["bokdAbleAt"] == "Y" else "예매 불가"
    seat_info = f"{event['restSeatCnt']}/{event['totSeatCnt']}석"

    # 이벤트 타입 표시
    event_type = event.get("eventDivCdNm") or ", ".join(event.get("matchedKeywords", [])) or "특별상영"

    # 예매 URL (모바일 - 앱으로 열림)
    booking_url = f"http://m.megabox.co.kr/booking?brchNo={event['brchNo']}&playDe={event['playDe']}&movieNo={event['movieNo']}"

    embed = {
        "embeds": [
            {
                "title": f"🎬 [{event_type}] 메가박스",
                "description": event["movieNm"],
                "url": booking_url,
                "color": 0x352263,  # 메가박스 보라색
                "fields": [
                    {"name": "📍 지점", "value": f"{event['areaCdNm']} {event['brchNm']}", "inline": True},
                    {"name": "📅 날짜", "value": formatted_date, "inline": True},
                    {"name": "⏰ 시간", "value": f"{event['playStartTime']} ~ {event['playEndTime']}", "inline": True},
                    {"name": "🎥 상영관", "value": event["theabExpoNm"], "inline": True},
                    {"name": "💺 좌석", "value": seat_info, "inline": True},
                    {"name": "🎫 상태", "value": bokd_status, "inline": True},
                ],
                "footer": {"text": "메가박스 이벤트 모니터"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    # 포스터 이미지 추가
    if event.get("moviePosterImg"):
        img_url = event["moviePosterImg"]
        if not img_url.startswith("http"):
            img_url = f"https://img.megabox.co.kr{img_url}"
        embed["embeds"][0]["thumbnail"] = {"url": img_url}

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            print(f"[{datetime.now()}] 알림 전송 완료: {event['movieNm']} @ {event['brchNm']}")
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

    print(f"[{datetime.now()}] 메가박스 이벤트 모니터링 시작...")
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL이 비어 있습니다 - 알림이 발송되지 않습니다. "
              "MEGABOX_DISCORD_WEBHOOK_URL 시크릿을 확인하세요")
    start_time = time.time()

    # 저장된 이벤트 불러오기
    saved_events = load_saved_events()
    is_first_run = len(saved_events) == 0

    if is_first_run:
        print(f"[{datetime.now()}] 첫 실행 - 기존 이벤트 수집 중...")

    # 전체 지점 목록 가져오기
    print(f"[{datetime.now()}] 지점 목록 조회 중...")
    branches = get_all_branches()
    print(f"[{datetime.now()}] 전체 지점 수: {len(branches)}")

    if not branches:
        print(f"[{datetime.now()}] 지점 목록을 가져올 수 없습니다.")
        return

    # 이벤트 상영 조회
    print(f"[{datetime.now()}] 이벤트 상영 조회 중 (14일간)...")
    current_events = fetch_events(branches, days=14)
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
            if event.get("areaCdNm") in TARGET_REGIONS:
                send_discord_notification(event)
                time.sleep(0.5)  # Discord rate limit 방지

    # 이벤트 저장 (기존 + 새로운)
    saved_events.update(current_events)

    # 오래된 이벤트 정리 (30일 이상 지난 이벤트 삭제)
    cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    saved_events = {k: v for k, v in saved_events.items() if v.get("playDe", "99999999") >= cutoff_date}

    save_events(saved_events)

    elapsed = time.time() - start_time
    print(f"[{datetime.now()}] 완료! 소요 시간: {elapsed:.1f}초")

    if is_first_run:
        print(f"[{datetime.now()}] 첫 실행 완료 - {len(current_events)}개 이벤트 저장됨")
        if DISCORD_WEBHOOK_URL:
            test_msg = {
                "content": f"✅ 메가박스 이벤트 모니터링이 시작되었습니다!\n현재 {len(current_events)}개의 이벤트 상영을 추적 중입니다."
            }
            try:
                requests.post(DISCORD_WEBHOOK_URL, json=test_msg, timeout=10)
            except:
                pass


if __name__ == "__main__":
    main()
