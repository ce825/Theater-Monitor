#!/Users/cehwang/miniconda3/bin/python3
"""
CGV 무대인사 모니터링 스크립트
주말(토/일) + 공휴일 무대인사 상영이 새로 등록되면 Discord로 알림
"""

import json
import os
import re
import random
import time
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# 설정
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1464630439116410963/NWuBIWCBPmlajS4sXmZ9P-P53OKmQt48rFt8im6Yo3NDkc4-ohC0SY6ZPt5R8C3Owp3y"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage_greetings.json")
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 타겟 극장 리스트: (지역, 극장명)
TARGET_THEATERS = [
    ("서울", "용산아이파크몰"),
    ("서울", "영등포타임스퀘어"),
    ("서울", "왕십리"),
    ("서울", "건대입구"),
    ("서울", "강변"),
    ("서울", "여의도"),
]

# 2026년 한국 공휴일 (월, 일) - 대체공휴일 포함
KOREAN_HOLIDAYS_2026 = [
    (1, 1),    # 신정
    (1, 28),   # 설날 연휴
    (1, 29),   # 설날
    (1, 30),   # 설날 연휴
    (3, 1),    # 삼일절
    (3, 2),    # 삼일절 대체공휴일
    (5, 5),    # 어린이날
    (5, 24),   # 부처님오신날
    (5, 25),   # 부처님오신날 대체공휴일
    (6, 6),    # 현충일
    (8, 15),   # 광복절
    (8, 17),   # 광복절 대체공휴일
    (9, 24),   # 추석 연휴
    (9, 25),   # 추석
    (9, 26),   # 추석 연휴
    (10, 3),   # 개천절
    (10, 5),   # 개천절 대체공휴일
    (10, 9),   # 한글날
    (12, 25),  # 크리스마스
]


def is_holiday(month, day):
    """해당 날짜가 공휴일인지 확인"""
    return (month, day) in KOREAN_HOLIDAYS_2026


def get_holidays_in_range(start_date, days=30):
    """주어진 기간 내의 공휴일 날짜 목록 반환"""
    holidays = []
    for i in range(days):
        check_date = start_date + timedelta(days=i)
        if is_holiday(check_date.month, check_date.day):
            day_name = ["월", "화", "수", "목", "금", "토", "일"][check_date.weekday()]
            holidays.append({
                "month": check_date.month,
                "day": check_date.day,
                "day_name": day_name
            })
    return holidays


def load_saved_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"greetings": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_discord_notification(greeting, notification_type="new"):
    """
    notification_type:
      - "new": 새로운 이벤트 등록
      - "preparing": 예매 준비중 상태 감지
      - "sales_started": 예매 시작 (준비중 → 예매 가능)
    """
    from datetime import timezone
    event_type = greeting.get("event_type", "무대인사")

    # 알림 유형별 설정
    if notification_type == "preparing":
        title = f"⏳ {event_type} 예매 준비중!"
        color = 0xFFA500  # 주황색
        footer_text = f"CGV {event_type} - 예매 준비중"
    elif notification_type == "sales_started":
        title = f"🎟️ {event_type} 예매 오픈!"
        color = 0x00FF00  # 초록색
        footer_text = f"CGV {event_type} - 예매 시작"
    else:
        title = f"🆕 새로운 {event_type} 일정 등록!"
        color = 0xED1C24  # CGV 빨간색
        footer_text = f"CGV {event_type} 알림"

    fields = [
        {"name": "🎬 영화", "value": greeting.get("movie", "미정"), "inline": False},
        {"name": "🎫 이벤트", "value": event_type, "inline": True},
        {"name": "📍 극장", "value": greeting.get("theater", "미정"), "inline": True},
        {"name": "📅 날짜", "value": greeting.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": greeting.get("time", "미정"), "inline": True},
    ]
    if greeting.get("hall"):
        fields.append({"name": "🎥 상영관", "value": greeting["hall"], "inline": True})

    embed = {
        "embeds": [{
            "title": title,
            "url": CGV_URL,
            "color": color,
            "fields": fields,
            "footer": {"text": footer_text},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            status_msg = {"preparing": "예매준비중", "sales_started": "예매오픈", "new": "신규"}
            print(f"  알림 전송 [{status_msg.get(notification_type, 'new')}]: {greeting['movie']} - {greeting['theater']} {greeting['date']} {greeting['time']}")
    except Exception as e:
        print(f"  Discord 오류: {e}")


def check_stage_greetings():
    """CGV 타겟 극장들의 주말 무대인사 확인"""
    all_greetings = []
    from datetime import timedelta

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()

            is_first_theater = True

            # 각 극장별로 확인
            for region, theater in TARGET_THEATERS:
                print(f"\n{'='*50}")
                print(f"[{region} > {theater}] 확인 중...")
                print('='*50)

                try:
                    # 1. 첫 극장만 URL 이동, 이후는 페이지 재사용
                    if is_first_theater:
                        page.goto(CGV_URL, timeout=60000)
                        # 페이지 로드 대기 (극장 선택 버튼이 나타날 때까지)
                        page.wait_for_selector("text=극장을 선택해 주세요", timeout=10000)
                        page.wait_for_timeout(1000)
                        is_first_theater = False

                    # 2. 극장 선택 팝업 열기
                    popup_opened = False
                    try:
                        page.click("text=극장을 선택해 주세요", timeout=2000)
                        popup_opened = True
                    except:
                        # 이미 극장이 선택된 상태 - 페이지 새로고침 후 다시 시도
                        page.goto(CGV_URL, timeout=60000)
                        page.wait_for_selector("text=극장을 선택해 주세요", timeout=10000)
                        page.wait_for_timeout(1000)
                        page.click("text=극장을 선택해 주세요", timeout=5000)
                        popup_opened = True
                    page.wait_for_timeout(800)

                    # 3. 로딩 오버레이 사라질 때까지 대기
                    try:
                        page.wait_for_selector(".loading_pageContainer__fvLY_", state="hidden", timeout=5000)
                    except:
                        pass

                    # 4. 지역 클릭 (숫자가 포함된 형태: 서울(29))
                    page.click(f"text=/{region}\\(\\d+\\)/", timeout=5000)
                    page.wait_for_timeout(500)

                    # 5. 극장 클릭
                    page.click(f"text={theater}", timeout=5000)
                    page.wait_for_timeout(500)

                    # 6. 극장선택 버튼 클릭
                    page.evaluate('''() => {
                        const elements = document.querySelectorAll('button, a, div, span');
                        for (const el of elements) {
                            const text = (el.innerText || '').trim();
                            if (text === '극장선택') {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }''')
                    # 날짜 캘린더가 로드될 때까지 대기
                    page.wait_for_timeout(1500)
                    print(f"  극장 선택 완료")

                    # 7. 모든 주말 + 공휴일 날짜 확인 (화살표 클릭으로 날짜 범위 확장)
                    checked_dates = set()  # 이미 확인한 날짜 추적
                    max_arrow_clicks = 10  # 최대 화살표 클릭 횟수 (무한 루프 방지)
                    arrow_clicks = 0

                    # 현재 월 기준 공휴일 목록 가져오기
                    current_holidays = get_holidays_in_range(datetime.now(), days=60)
                    holiday_dates = {(h["day_name"], h["day"]) for h in current_holidays}

                    while arrow_clicks <= max_arrow_clicks:
                        # JavaScript로 캘린더에서 모든 날짜 추출 (주말 + 공휴일 필터링용)
                        all_dates = page.evaluate("""() => {
                            var results = [];
                            // 캘린더 영역 상단 350px 이내의 요소만 검색
                            var elements = document.querySelectorAll('li, button, div, span, a');
                            for (var i = 0; i < elements.length; i++) {
                                var el = elements[i];
                                var rect = el.getBoundingClientRect();
                                // 캘린더는 상단에 위치 (y: 50~350)
                                if (rect.top < 50 || rect.top > 350) continue;
                                if (rect.height < 10 || rect.height > 80) continue;

                                var text = (el.innerText || '').trim();
                                // 모든 요일 패턴 매칭 (월, 화, 수, 목, 금, 토, 일)
                                var match = text.match(/^(월|화|수|목|금|토|일)\\n(\\d{1,2})$/);
                                if (match) {
                                    results.push({day: match[1], date: match[2].replace(/^0/, '') || '0'});
                                }
                            }
                            return results;
                        }""")

                        # 주말 또는 공휴일만 필터링
                        weekend_dates = []
                        for d in all_dates:
                            is_weekend = d['day'] in ['토', '일']
                            is_holiday_date = (d['day'], int(d['date'])) in holiday_dates
                            if is_weekend or is_holiday_date:
                                weekend_dates.append(d)

                        # 중복 제거 및 정렬 (날짜순)
                        seen = set()
                        unique_dates = []
                        for d in weekend_dates:
                            key = f"{d['day']}_{d['date']}"
                            if key not in seen:
                                seen.add(key)
                                unique_dates.append(d)
                        weekend_dates = sorted(unique_dates, key=lambda x: int(x['date']))

                        found_dates = [d['day'] + d['date'] for d in weekend_dates]
                        print(f"  발견된 주말/공휴일: {found_dates}")

                        # 새로운 주말 날짜가 없으면 종료
                        new_dates = [d for d in weekend_dates if f"{d['day']}_{d['date']}" not in checked_dates]
                        if not new_dates:
                            if arrow_clicks == 0 and not weekend_dates:
                                # 첫 시도에서 주말 날짜가 없으면 화살표 클릭 후 재시도
                                pass
                            else:
                                print(f"  더 이상 새로운 주말/공휴일 날짜 없음 → 다음 극장")
                                break

                        # 새로운 날짜만 확인 (이미 체크한 날짜 건너뛰기)
                        for date_info in new_dates:
                            day = date_info["day"]
                            date_num = date_info["date"]
                            date_key = f"{day}_{date_num}"
                            checked_dates.add(date_key)

                            try:
                                # 해당 날짜 클릭 (Playwright locator 사용)
                                date_clicked = False

                                # 날짜 패턴들 시도 (07, 7 등 다양한 형태)
                                date_padded = date_num.zfill(2)
                                patterns = [
                                    f"text=/{day}\\n{date_padded}$/",
                                    f"text=/{day}\\n{date_num}$/",
                                    f"text=/{day}.*{date_padded}/",
                                    f"text=/{day}.*{date_num}/"
                                ]

                                # 먼저 JavaScript로 날짜 요소를 화면에 스크롤
                                scroll_result = page.evaluate(
                                    """(args) => {
                                    var day = args.day;
                                    var dateNum = args.dateNum;
                                    var datePadded = args.datePadded;
                                    var items = document.querySelectorAll('li, button, div, span, a');
                                    for (var i = 0; i < items.length; i++) {
                                        var item = items[i];
                                        var rect = item.getBoundingClientRect();
                                        if (rect.top > 350 || rect.top < 0) continue;
                                        var text = (item.innerText || '').trim();
                                        if (text === day + '\\n' + datePadded ||
                                            text === day + '\\n' + dateNum) {
                                            item.scrollIntoView({behavior: 'instant', block: 'center', inline: 'center'});
                                            return {found: true, text: text};
                                        }
                                    }
                                    return {found: false};
                                }""", {"day": day, "dateNum": date_num, "datePadded": date_padded})

                                if scroll_result.get("found"):
                                    page.wait_for_timeout(200)

                                # 날짜 클릭 시도
                                date_disabled = False
                                for pattern in patterns:
                                    if date_clicked:
                                        break
                                    try:
                                        locator = page.locator(pattern).first
                                        if locator.is_visible(timeout=1000):
                                            # disabled 체크 (부모 요소까지 확인)
                                            is_disabled = locator.evaluate("""el => {
                                                if (el.disabled || el.className.includes('disabled')) return true;
                                                var parent = el.parentElement;
                                                for (var i = 0; i < 3 && parent; i++) {
                                                    if (parent.disabled || parent.className.includes('disabled')) return true;
                                                    var style = window.getComputedStyle(parent);
                                                    if (style.opacity < 0.5 || style.pointerEvents === 'none') return true;
                                                    parent = parent.parentElement;
                                                }
                                                var myStyle = window.getComputedStyle(el);
                                                if (myStyle.opacity < 0.5 || myStyle.pointerEvents === 'none') return true;
                                                return false;
                                            }""")
                                            if not is_disabled:
                                                locator.click(timeout=3000)
                                                date_clicked = True
                                                print(f"    날짜 클릭: {day} {date_num}")
                                            else:
                                                date_disabled = True
                                                print(f"    날짜 비활성: {day} {date_num}")
                                    except:
                                        pass

                                # 비활성 날짜는 스킵 (JS 클릭 시도하지 않음)
                                if date_disabled:
                                    print(f"    날짜 스킵(비활성): {day} {date_num}")
                                    continue

                                # 여전히 클릭 안 되면 JavaScript로 직접 클릭 시도
                                if not date_clicked:
                                    js_click = page.evaluate(
                                        """(args) => {
                                        var day = args.day;
                                        var dateNum = args.dateNum;
                                        var datePadded = args.datePadded;
                                        var items = document.querySelectorAll('li, button, div, span, a');
                                        for (var i = 0; i < items.length; i++) {
                                            var item = items[i];
                                            var rect = item.getBoundingClientRect();
                                            if (rect.top > 350 || rect.top < 0) continue;
                                            var text = (item.innerText || '').trim();
                                            if (text === day + '\\n' + datePadded ||
                                                text === day + '\\n' + dateNum) {
                                                // 비활성 상태 체크 (부모 포함)
                                                var disabled = item.disabled || item.className.includes('disabled');
                                                var parent = item.parentElement;
                                                for (var j = 0; j < 3 && parent && !disabled; j++) {
                                                    if (parent.disabled || parent.className.includes('disabled')) disabled = true;
                                                    var style = window.getComputedStyle(parent);
                                                    if (parseFloat(style.opacity) < 0.5 || style.pointerEvents === 'none') disabled = true;
                                                    parent = parent.parentElement;
                                                }
                                                var myStyle = window.getComputedStyle(item);
                                                if (parseFloat(myStyle.opacity) < 0.5 || myStyle.pointerEvents === 'none') disabled = true;

                                                if (!disabled) {
                                                    item.click();
                                                    return {clicked: true, text: text, top: rect.top};
                                                } else {
                                                    return {clicked: false, disabled: true};
                                                }
                                            }
                                        }
                                        return {clicked: false, notFound: true};
                                    }""", {"day": day, "dateNum": date_num, "datePadded": date_padded})

                                    if js_click.get("clicked"):
                                        date_clicked = True
                                        print(f"    날짜 클릭(JS): {day} {date_num}")
                                    elif js_click.get("disabled"):
                                        print(f"    날짜 스킵(비활성): {day} {date_num}")
                                        continue

                                if not date_clicked:
                                    print(f"    날짜 스킵: {day} {date_num}")
                                    continue
                                page.wait_for_timeout(1200)

                                # 페이지 스크롤하여 모든 영화 로드 (lazy loading 대응)
                                page.evaluate("""() => {
                                    window.scrollTo(0, document.body.scrollHeight);
                                }""")
                                page.wait_for_timeout(600)
                                page.evaluate("""() => {
                                    window.scrollTo(0, 0);
                                }""")
                                page.wait_for_timeout(400)

                                # 상영 시간표에서 영화별 무대인사/GV/시네마톡 추출
                                movie_events = page.evaluate("""() => {
                                    var results = [];
                                    var movieSections = document.querySelectorAll('[class*="movie"], [class*="Movie"], .time-table-wrap, .sect-showtimes');

                                    if (movieSections.length === 0) {
                                        movieSections = document.querySelectorAll('body > div');
                                    }

                                    var bodyText = document.body.innerText;
                                    var lines = bodyText.split('\\n');
                                    var currentMovie = '';
                                    var currentTimes = [];
                                    var inTimeSection = false;

                                    for (var i = 0; i < lines.length; i++) {
                                        var line = lines[i].trim();

                                        // Skip empty lines and common UI elements
                                        if (!line || line.length < 2) continue;
                                        if (/^(전체|오전|오후|18시|심야|영화순|시간순|예매|CGV|2D|3D|IMAX|Laser|관$)/.test(line)) continue;

                                        // Detect movie title (Korean text, not time, not seat info)
                                        var excludeWords = /^(더빙|자막|조조|매진|마감|예매종료|잔여|좌석|개봉|전체|오전|오후|심야|영화순|시간순|예매|일반|특별관|필름|디지털|재개봉|재상영|N차상영|기획전|영화제|시사회|쿠키|스페셜|한정|단독|독점|라이브뷰잉|응원상영|싱어롱|절찬|대개봉|개봉작|상영작|상영중|상영예정|CGV|2D|3D|IMAX|Laser|\d+관|DOLBY|ATMOS|SCREENX|4DX|리클라이너|아트하우스)$/;
                                        if (/^[가-힣]/.test(line) && !/^\d/.test(line) && !/석$/.test(line) && !/(무대인사|시네마톡|GV)/.test(line) && line.length >= 2 && line.length <= 30) {
                                            if (!excludeWords.test(line)) {
                                                // Save previous movie if it had events
                                                if (currentMovie && currentTimes.length > 0) {
                                                    for (var t = 0; t < currentTimes.length; t++) {
                                                        results.push({movie: currentMovie, time: currentTimes[t].time, eventType: currentTimes[t].eventType, preparing: currentTimes[t].preparing || false});
                                                    }
                                                }
                                                currentMovie = line;
                                                currentTimes = [];
                                            }
                                        }

                                        // Detect time with event tag (e.g., "14:30" followed by "무대인사")
                                        var timeMatch = line.match(/^(\d{1,2}:\d{2})/);
                                        if (timeMatch && currentMovie) {
                                            var timeStr = timeMatch[1];
                                            // Check next few lines for event tags and status
                                            var hasEvent = false;
                                            var eventType = '';
                                            var isPreparing = false;
                                            for (var j = i; j < Math.min(i + 5, lines.length); j++) {
                                                var checkLine = lines[j];
                                                if (checkLine.indexOf('예매 준비중') !== -1 || checkLine.indexOf('예매준비중') !== -1) {
                                                    isPreparing = true;
                                                }
                                                if (checkLine.indexOf('무대인사') !== -1) {
                                                    hasEvent = true;
                                                    eventType = '무대인사';
                                                }
                                                if (checkLine.indexOf('시네마톡') !== -1) {
                                                    hasEvent = true;
                                                    eventType = '시네마톡';
                                                }
                                                // GV 감지 비활성화 - CGV 페이지에서 오탐지가 너무 많음
                                                // 실제 GV 이벤트는 대부분 "시네마톡"이나 "무대인사"로 표시됨
                                                // if (checkLine.trim() === 'GV') { ... }
                                                // Stop if we hit another time or movie
                                                if (j > i && /^\d{1,2}:\d{2}/.test(lines[j])) break;
                                            }
                                            if (hasEvent) {
                                                currentTimes.push({time: timeStr, eventType: eventType, preparing: isPreparing});
                                            }
                                        }
                                    }

                                    // Don't forget last movie
                                    if (currentMovie && currentTimes.length > 0) {
                                        for (var t = 0; t < currentTimes.length; t++) {
                                            results.push({movie: currentMovie, time: currentTimes[t].time, eventType: currentTimes[t].eventType, preparing: currentTimes[t].preparing || false});
                                        }
                                    }

                                    return results;
                                }""")

                                if movie_events and len(movie_events) > 0:
                                    print(f"  ★ {day}요일 {date_num}일 이벤트 발견: {len(movie_events)}건")

                                    # 날짜 계산
                                    today = datetime.now()
                                    target_day = int(date_num)

                                    if target_day >= today.day:
                                        current_month = today.month
                                        current_year = today.year
                                    else:
                                        if today.month == 12:
                                            current_month = 1
                                            current_year = today.year + 1
                                        else:
                                            current_month = today.month + 1
                                            current_year = today.year

                                    date_str = f"{current_month}월 {date_num}일 ({day})"

                                    for event in movie_events:
                                        movie_name = event.get("movie", "미정")
                                        time_str = event.get("time", "")
                                        event_type = event.get("eventType", "무대인사")
                                        is_preparing = event.get("preparing", False)

                                        greeting_id = f"{theater}_{current_year}_{current_month}_{date_num}_{time_str}_{movie_name[:10]}"

                                        if greeting_id not in [x["id"] for x in all_greetings]:
                                            status_str = " [예매준비중]" if is_preparing else ""
                                            print(f"    - [{event_type}] {movie_name} {time_str}{status_str}")
                                            g = {
                                                "movie": movie_name,
                                                "theater": f"CGV {theater}",
                                                "date": date_str,
                                                "time": time_str,
                                                "hall": "",
                                                "event_type": event_type,
                                                "id": greeting_id,
                                                "preparing": is_preparing
                                            }
                                            all_greetings.append(g)
                                else:
                                    print(f"  {day}요일 {date_num}일 이벤트 없음")
                            except Exception as e:
                                print(f"  {day}요일 {date_num}일 오류: {e}")

                        # 화살표 버튼 클릭하여 다음 날짜 범위로 이동
                        # 날짜 영역 상단(y < 300)에 있는 ">" 버튼만 클릭
                        arrow_clicked = page.evaluate(
                            """() => {
                            const arrows = document.querySelectorAll('button, a, div, span');
                            for (const el of arrows) {
                                const text = (el.innerText || '').trim();
                                const rect = el.getBoundingClientRect();
                                // 상단 날짜 영역(y < 300)에 있고, ">" 문자인 경우만
                                if (rect.top < 300 && rect.top > 0 && (text === '>' || text === String.fromCharCode(8250))) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""")

                        if not arrow_clicked:
                            print(f"  화살표 버튼 없음 → 다음 극장")
                            break

                        arrow_clicks += 1
                        print(f"  → 다음 날짜 범위로 이동 ({arrow_clicks})")
                        page.wait_for_timeout(800)

                except Exception as e:
                    print(f"  [{theater}] 오류: {e}")
                    continue

            browser.close()
            print("\n" + "="*50)
            print("모든 극장 확인 완료!")
            return all_greetings

    except Exception as e:
        print(f"오류: {e}")
        return all_greetings


def check_stage_greetings_old():
    """이전 버전 - 사용 안함"""
    all_greetings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled']
            )
            stealth = Stealth()
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            stealth.apply_stealth_sync(context)
            page = context.new_page()

            page.goto(CGV_URL, timeout=60000)
            page.wait_for_timeout(6000)

            movie_imgs = page.query_selector_all("img[alt]")
            movies = []
            for img in movie_imgs:
                alt = img.get_attribute("alt") or ""
                if alt and alt not in ["CGV", ""] and len(alt) > 1 and not alt.startswith("http"):
                    movies.append(alt)
            movies = list(dict.fromkeys(movies))[:10]
            print(f"[{datetime.now()}] 영화 {len(movies)}개: {movies[:3]}...")

            for movie_name in movies:
                try:
                    print(f"\n  [{movie_name}] 확인 중...")
                    page.goto(CGV_URL, timeout=30000)
                    page.wait_for_timeout(5000)

                    movie_img = page.query_selector(f"img[alt='{movie_name}']")
                    if not movie_img:
                        continue
                    movie_img.click(force=True)
                    page.wait_for_timeout(4000)

                    for region, theater in TARGET_THEATERS:
                        try:
                            # 극장 선택 팝업 열기
                            try:
                                page.click("text=선택 된 극장이 없습니다", force=True, timeout=3000)
                            except:
                                try:
                                    page.click("text=극장을 선택해 주세요", force=True, timeout=2000)
                                except:
                                    page.click("text=자주가는 CGV", force=True, timeout=2000)
                            page.wait_for_timeout(3000)

                            # "지역별" 탭 클릭
                            try:
                                page.click("text=지역별", force=True, timeout=2000)
                                page.wait_for_timeout(1500)
                            except:
                                pass

                            # 지역 클릭
                            try:
                                page.click(f"text=/{region}\\(\\d+\\)/", force=True, timeout=3000)
                            except:
                                page.click(f"text={region}", force=True, timeout=3000)
                            page.wait_for_timeout(2000)

                            # 극장 클릭
                            page.click(f"text=\"{theater}\"", force=True, timeout=3000)
                            page.wait_for_timeout(1500)

                            # 극장선택 버튼 클릭
                            page.click("button:has-text('극장선택')", force=True, timeout=3000)
                            page.wait_for_timeout(4000)
                            print(f"    {region} > {theater} 극장 선택 완료")

                            # 현재 페이지에서 무대인사 확인 (날짜 클릭 없이)
                            today = datetime.now()
                            today_day = today.day
                            day_type = "일" if today.weekday() == 6 else "토"

                            body_text = page.inner_text("body")
                            if "무대인사" in body_text:
                                lines = body_text.split('\n')
                                hall = ""
                                for i, line in enumerate(lines):
                                    line = line.strip()
                                    if re.search(r'\d+관|IMAX|Laser', line):
                                        hall = line[:30]
                                    if line == "무대인사":
                                        for j in range(max(0, i-6), i):
                                            tm = re.search(r'(\d{1,2}:\d{2})', lines[j])
                                            if tm:
                                                g = {
                                                    "movie": "미정",
                                                    "theater": f"CGV {theater}",
                                                    "date": f"{today_day}일({day_type})",
                                                    "time": tm.group(1),
                                                    "hall": hall,
                                                    "id": f"{theater}_{today_day}_{tm.group(1)}"
                                                }
                                                if g["id"] not in [x["id"] for x in all_greetings]:
                                                    all_greetings.append(g)
                                                    print(f"        ★ 무대인사: {g['date']} {g['time']}")
                                                break

                            # 메인 페이지로 돌아가기
                            page.goto(CGV_URL, timeout=30000)
                            page.wait_for_timeout(4000)

                        except Exception as e:
                            print(f"    {theater} 오류: {e}")
                            page.goto(CGV_URL, timeout=30000)
                            page.wait_for_timeout(3000)

                except Exception as e:
                    print(f"    오류: {e}")
                    continue

            browser.close()

    except Exception as e:
        print(f"[{datetime.now()}] 브라우저 오류: {e}")
        return None

    return all_greetings


def main():
    # 랜덤 딜레이 (0~60초) - 봇 패턴 회피
    delay = random.randint(0, 60)
    print(f"[{datetime.now()}] 랜덤 딜레이: {delay}초")
    time.sleep(delay)

    print(f"[{datetime.now()}] CGV 주말 무대인사 모니터링 시작...")

    saved_data = load_saved_data()
    saved_ids = set(g.get("id", "") for g in saved_data.get("greetings", []))

    greetings = check_stage_greetings()

    if greetings is None:
        print(f"[{datetime.now()}] 조회 실패")
        return

    print(f"\n[{datetime.now()}] 총 {len(greetings)}개 무대인사 발견")

    # 첫 실행
    if not saved_data.get("greetings"):
        print(f"[{datetime.now()}] 첫 실행 - 저장 중...")
        saved_data["greetings"] = greetings
        save_data(saved_data)

        if greetings:
            msg = {"content": f"✅ CGV 무대인사 모니터링 시작!\n현재 {len(greetings)}개 주말 무대인사 추적 중"}
            try:
                requests.post(DISCORD_WEBHOOK_URL, json=msg, timeout=10)
            except:
                pass
        return

    # 기존 이벤트를 ID로 매핑
    saved_by_id = {g.get("id"): g for g in saved_data.get("greetings", []) if g.get("id")}

    new_greetings = []
    preparing_greetings = []  # 새로 감지된 예매 준비중
    sales_started_greetings = []  # 예매 준비중 → 예매 시작

    for g in greetings:
        gid = g.get("id")
        if not gid:
            continue

        if gid not in saved_ids:
            # 새로운 이벤트
            new_greetings.append(g)
            if g.get("preparing"):
                preparing_greetings.append(g)
        else:
            # 기존 이벤트 - 상태 변경 확인
            old_g = saved_by_id.get(gid)
            if old_g and old_g.get("preparing") and not g.get("preparing"):
                # 예매 준비중 → 예매 시작
                sales_started_greetings.append(g)
                # 기존 데이터 업데이트
                old_g["preparing"] = False

    # 알림 전송
    if new_greetings:
        print(f"[{datetime.now()}] 새 이벤트 {len(new_greetings)}개!")
        for g in new_greetings:
            if g.get("preparing"):
                send_discord_notification(g, "preparing")
            else:
                send_discord_notification(g, "new")
        saved_data["greetings"].extend(new_greetings)

    if sales_started_greetings:
        print(f"[{datetime.now()}] 예매 오픈 {len(sales_started_greetings)}개!")
        for g in sales_started_greetings:
            send_discord_notification(g, "sales_started")

    if new_greetings or sales_started_greetings:
        save_data(saved_data)
    else:
        print(f"[{datetime.now()}] 새 무대인사 없음")


if __name__ == "__main__":
    main()
