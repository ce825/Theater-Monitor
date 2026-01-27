#!/usr/bin/env python3
"""
CGV 무대인사/GV/시네마톡 모니터링 (GitHub Actions용)
"""

import json
import os
import re
import random
import time
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = "stage_greetings.json"
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 타겟 극장 리스트: (지역, 극장명)
TARGET_THEATERS = [
    ("서울", "용산아이파크몰"),
    ("서울", "영등포"),
    ("서울", "왕십리"),
    ("서울", "건대입구"),
    ("서울", "강변"),
    ("서울", "여의도"),
]


def load_saved_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"greetings": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_discord_notification(greeting):
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook URL not set")
        return

    event_type = greeting.get("event_type", "무대인사")

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
            "title": f"새로운 {event_type} 일정이 등록되었습니다!",
            "url": CGV_URL,
            "color": 0xED1C24,  # CGV 빨간색
            "fields": fields,
            "footer": {"text": f"CGV {event_type} 알림"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            print(f"  알림 전송: {greeting['movie']} - {greeting['theater']} {greeting['date']} {greeting['time']}")
    except Exception as e:
        print(f"  Discord 오류: {e}")


def check_stage_greetings():
    """CGV 타겟 극장들의 주말 무대인사/GV/시네마톡 확인"""
    all_greetings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            stealth = Stealth()
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}
            )
            stealth.apply_stealth_sync(context)
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
                        page.wait_for_timeout(3000)

                        # Cloudflare 체크
                        if "Cloudflare" in page.title() or "Attention" in page.title():
                            print("  Cloudflare 감지 - 대기 중...")
                            page.wait_for_timeout(10000)

                        page.wait_for_selector("text=극장을 선택해 주세요", timeout=10000)
                        page.wait_for_timeout(500)
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

                    # 4. 지역 클릭
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

                    # 7. 모든 주말 날짜 확인 (화살표 클릭으로 날짜 범위 확장)
                    checked_dates = set()
                    max_arrow_clicks = 10
                    arrow_clicks = 0

                    while arrow_clicks <= max_arrow_clicks:
                        # 페이지 전체 텍스트에서 주말 날짜 찾기
                        page_text = page.inner_text("body")
                        weekend_dates = []

                        for match in re.finditer(r'토\s*\n?\s*(\d{1,2})', page_text):
                            date_num = match.group(1).lstrip('0') or '0'
                            weekend_dates.append({"day": "토", "date": date_num})
                        for match in re.finditer(r'일\s*\n?\s*(\d{1,2})', page_text):
                            date_num = match.group(1).lstrip('0') or '0'
                            weekend_dates.append({"day": "일", "date": date_num})

                        # 중복 제거 및 정렬
                        seen = set()
                        unique_dates = []
                        for d in weekend_dates:
                            key = f"{d['day']}_{d['date']}"
                            if key not in seen:
                                seen.add(key)
                                unique_dates.append(d)
                        weekend_dates = sorted(unique_dates, key=lambda x: int(x['date']))

                        found_dates = [d['day'] + d['date'] for d in weekend_dates]
                        print(f"  발견된 주말: {found_dates}")

                        # 새로운 주말 날짜가 없으면 종료
                        new_dates = [d for d in weekend_dates if f"{d['day']}_{d['date']}" not in checked_dates]
                        if not new_dates:
                            if arrow_clicks == 0 and not weekend_dates:
                                pass
                            else:
                                print(f"  더 이상 새로운 주말 날짜 없음 → 다음 극장")
                                break

                        # 새로운 날짜만 확인
                        for date_info in new_dates:
                            day = date_info["day"]
                            date_num = date_info["date"]
                            date_key = f"{day}_{date_num}"
                            checked_dates.add(date_key)

                            try:
                                date_clicked = False
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

                                # JavaScript로 직접 클릭 시도
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

                                # 페이지 스크롤하여 모든 영화 로드
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
                                                        results.push({movie: currentMovie, time: currentTimes[t].time, eventType: currentTimes[t].eventType});
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
                                            // Check next few lines for event tags
                                            var hasEvent = false;
                                            var eventType = '';
                                            for (var j = i; j < Math.min(i + 5, lines.length); j++) {
                                                var checkLine = lines[j];
                                                if (checkLine.indexOf('무대인사') !== -1) {
                                                    hasEvent = true;
                                                    eventType = '무대인사';
                                                    break;
                                                }
                                                if (checkLine.indexOf('시네마톡') !== -1) {
                                                    hasEvent = true;
                                                    eventType = '시네마톡';
                                                    break;
                                                }
                                                if (/(?<!C)GV(?!C)/.test(checkLine) && checkLine.indexOf('CGV') === -1) {
                                                    hasEvent = true;
                                                    eventType = 'GV';
                                                    break;
                                                }
                                                // Stop if we hit another time or movie
                                                if (j > i && /^\d{1,2}:\d{2}/.test(lines[j])) break;
                                            }
                                            if (hasEvent) {
                                                currentTimes.push({time: timeStr, eventType: eventType});
                                            }
                                        }
                                    }

                                    // Don't forget last movie
                                    if (currentMovie && currentTimes.length > 0) {
                                        for (var t = 0; t < currentTimes.length; t++) {
                                            results.push({movie: currentMovie, time: currentTimes[t].time, eventType: currentTimes[t].eventType});
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

                                        greeting_id = f"{theater}_{current_year}_{current_month}_{date_num}_{time_str}_{movie_name[:10]}"

                                        if greeting_id not in [x["id"] for x in all_greetings]:
                                            print(f"    - [{event_type}] {movie_name} {time_str}")
                                            g = {
                                                "movie": movie_name,
                                                "theater": f"CGV {theater}",
                                                "date": date_str,
                                                "time": time_str,
                                                "hall": "",
                                                "event_type": event_type,
                                                "id": greeting_id
                                            }
                                            all_greetings.append(g)
                                else:
                                    print(f"  {day}요일 {date_num}일 이벤트 없음")
                            except Exception as e:
                                print(f"  {day}요일 {date_num}일 오류: {e}")

                        # 화살표 버튼 클릭하여 다음 날짜 범위로 이동
                        arrow_clicked = page.evaluate(
                            """() => {
                            const arrows = document.querySelectorAll('button, a, div, span');
                            for (const el of arrows) {
                                const text = (el.innerText || '').trim();
                                const rect = el.getBoundingClientRect();
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
                    # 디버그 스크린샷 저장
                    try:
                        page.screenshot(path="debug_screenshot.png")
                        print("  디버그 스크린샷 저장됨")
                    except:
                        pass
                    continue

            browser.close()
            print("\n" + "="*50)
            print("모든 극장 확인 완료!")

    except Exception as e:
        print(f"브라우저 오류: {e}")
        return None

    return all_greetings


def main():
    # 랜덤 딜레이 (0~60초) - 봇 패턴 회피
    delay = random.randint(0, 60)
    print(f"[{datetime.now()}] 랜덤 딜레이: {delay}초")
    time.sleep(delay)

    print(f"[{datetime.now()}] CGV 무대인사/GV/시네마톡 모니터링 시작...")

    saved_data = load_saved_data()
    saved_ids = set(g.get("id", "") for g in saved_data.get("greetings", []))

    greetings = check_stage_greetings()

    if greetings is None:
        print("조회 실패")
        return

    print(f"\n총 {len(greetings)}개 이벤트 발견")

    if not saved_data.get("greetings"):
        print("첫 실행 - 저장")
        saved_data["greetings"] = greetings
        save_data(saved_data)
        if greetings and DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json={
                "content": f"✅ CGV 무대인사/GV/시네마톡 모니터링 시작!\n{len(greetings)}개 이벤트 추적 중"
            }, timeout=10)
        return

    new_greetings = [g for g in greetings if g.get("id") and g["id"] not in saved_ids]

    if new_greetings:
        print(f"새 이벤트 {len(new_greetings)}개!")
        for g in new_greetings:
            send_discord_notification(g)
        saved_data["greetings"].extend(new_greetings)
        save_data(saved_data)
    else:
        print("새 이벤트 없음")


if __name__ == "__main__":
    main()
