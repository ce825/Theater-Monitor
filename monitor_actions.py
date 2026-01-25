#!/usr/bin/env python3
"""
CGV 무대인사/GV/시네마톡 모니터링 (GitHub Actions용)
"""

import json
import os
import re
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
            "color": 5814783,
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

            # 각 극장별로 확인
            for region, theater in TARGET_THEATERS:
                print(f"\n{'='*50}")
                print(f"[{region} > {theater}] 확인 중...")
                print('='*50)

                try:
                    # 1. CGV 예매 페이지 이동
                    page.goto(CGV_URL, timeout=60000)
                    page.wait_for_timeout(5000)

                    # Cloudflare 체크
                    if "Cloudflare" in page.title() or "Attention" in page.title():
                        print("  Cloudflare 감지 - 대기 중...")
                        page.wait_for_timeout(10000)

                    # 2. 극장 선택 팝업 열기
                    page.click("text=극장을 선택해 주세요", timeout=5000)
                    page.wait_for_timeout(2000)

                    # 3. 지역 클릭
                    page.click(f"text=/{region}\\(\\d+\\)/", timeout=5000)
                    page.wait_for_timeout(1500)

                    # 4. 극장 클릭
                    page.click(f"text={theater}", timeout=5000)
                    page.wait_for_timeout(1500)

                    # 5. 극장선택 버튼 클릭
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
                    page.wait_for_timeout(4000)
                    print(f"  극장 선택 완료")

                    # 6. 모든 주말 날짜 확인 (화살표 클릭으로 날짜 범위 확장)
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
                                    const day = args.day;
                                    const dateNum = args.dateNum;
                                    const datePadded = args.datePadded;
                                    const items = document.querySelectorAll('li, button, div, span, a');
                                    for (const item of items) {
                                        const text = (item.innerText || '').trim();
                                        if (text === day + '\\n' + datePadded ||
                                            text === day + '\\n' + dateNum ||
                                            text === day + ' ' + datePadded ||
                                            text === day + ' ' + dateNum) {
                                            item.scrollIntoView({behavior: 'instant', block: 'center', inline: 'center'});
                                            return {found: true, text: text};
                                        }
                                    }
                                    return {found: false};
                                }""", {"day": day, "dateNum": date_num, "datePadded": date_padded})

                                if scroll_result.get("found"):
                                    page.wait_for_timeout(500)

                                # 날짜 클릭 시도
                                for pattern in patterns:
                                    if date_clicked:
                                        break
                                    try:
                                        locator = page.locator(pattern).first
                                        if locator.is_visible(timeout=1000):
                                            is_disabled = locator.evaluate("el => el.disabled || el.className.includes('disabled')")
                                            if not is_disabled:
                                                locator.click(timeout=3000)
                                                date_clicked = True
                                                print(f"    날짜 클릭: {day} {date_num}")
                                            else:
                                                print(f"    날짜 비활성: {day} {date_num}")
                                    except:
                                        pass

                                # JavaScript로 직접 클릭 시도
                                if not date_clicked:
                                    js_click = page.evaluate(
                                        """(args) => {
                                        const day = args.day;
                                        const dateNum = args.dateNum;
                                        const datePadded = args.datePadded;
                                        const items = document.querySelectorAll('li, button, div, span, a');
                                        for (const item of items) {
                                            const text = (item.innerText || '').trim();
                                            if (text === day + '\\n' + datePadded ||
                                                text === day + '\\n' + dateNum ||
                                                text === day + ' ' + datePadded ||
                                                text === day + ' ' + dateNum) {
                                                if (!item.disabled && !item.className.includes('disabled')) {
                                                    item.click();
                                                    return {clicked: true, text: text};
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
                                        print(f"    날짜 비활성: {day} {date_num}")

                                if not date_clicked:
                                    print(f"    날짜 스킵: {day} {date_num}")
                                    continue
                                page.wait_for_timeout(3000)

                                # 페이지 스크롤하여 모든 영화 로드
                                page.evaluate("""() => {
                                    window.scrollTo(0, document.body.scrollHeight);
                                }""")
                                page.wait_for_timeout(1500)
                                page.evaluate("""() => {
                                    window.scrollTo(0, 0);
                                }""")
                                page.wait_for_timeout(1000)

                                # 무대인사/GV/시네마톡 확인
                                body = page.inner_text("body")
                                found_events = []
                                if "무대인사" in body:
                                    found_events.append("무대인사")
                                if "시네마톡" in body:
                                    found_events.append("시네마톡")
                                # GV는 독립 단어로만 검색 (CGV 오탐 방지)
                                if re.search(r'(?<!C)GV(?!C)', body):
                                    found_events.append("GV")

                                if found_events:
                                    print(f"  ★ {day}요일 {date_num}일 이벤트 발견: {', '.join(found_events)}")

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

                                    # 시간 및 영화 제목 추출
                                    lines = body.split('\n')
                                    exclude_words = ["무대인사", "GV", "시네마톡", "전체", "오전", "오후", "18시 이후", "심야", theater, "예매", "상영시간표", "예매종료", "매진", "영화순", "시간순", "극장별 예매", "영화별예매"]
                                    hall_patterns = r'(DOLBY|ATMOS|SCREENX|SOUNDX|4DX|IMAX|SPHERE|Laser|리클라이너|아트하우스|\d+관|2D|3D|전도연관|씨네앤포레|씨네\&포레|CINE|MX관|GOLD CLASS|SUITE CINEMA|PREMIUM|TEMPUR|STARIUM|CGV|특별관|일반|조조)'

                                    movie_candidates = []
                                    for idx, line in enumerate(lines):
                                        text = line.strip()
                                        if len(text) >= 2 and re.search(r'[가-힣]', text):
                                            if not re.match(r'^[\d:~\-\(\)\[\]관]', text):
                                                if text not in exclude_words:
                                                    if not re.search(r'(석|좌석|잔여|매진|마감|\d+:\d+|~|개봉)', text):
                                                        if not re.search(hall_patterns, text, re.IGNORECASE):
                                                            movie_candidates.append((idx, text))

                                    # 이벤트 키워드가 포함된 모든 줄 찾기
                                    found_times = set()
                                    for i, line in enumerate(lines):
                                        line_stripped = line.strip()
                                        if any(kw in line_stripped for kw in event_keywords):
                                            tm_same = re.search(r'(\d{1,2}:\d{2})', line_stripped)
                                            if tm_same:
                                                found_times.add((i, tm_same.group(1)))
                                            for j in range(max(0, i-5), i):
                                                tm = re.search(r'(\d{1,2}:\d{2})', lines[j])
                                                if tm:
                                                    found_times.add((i, tm.group(1)))

                                    # 각 시간에 대해 영화 정보 추출
                                    for (line_idx, time_str) in found_times:
                                        movie_name = ""

                                        for k in range(line_idx-1, max(0, line_idx-40), -1):
                                            candidate = lines[k].strip()
                                            if len(candidate) >= 2 and re.search(r'[가-힣]', candidate):
                                                if not re.match(r'^[\d:~\-\(\)\[\]관]', candidate):
                                                    if candidate not in exclude_words:
                                                        if not re.search(r'(석|좌석|잔여|매진|마감|\d+:\d+|~|개봉)', candidate):
                                                            if not re.search(hall_patterns, candidate, re.IGNORECASE):
                                                                movie_name = candidate
                                                                break

                                        if not movie_name and movie_candidates:
                                            closest = min(movie_candidates, key=lambda x: abs(x[0] - line_idx))
                                            if abs(closest[0] - line_idx) < 50:
                                                movie_name = closest[1]

                                        movie_final = movie_name if movie_name else found_events[0]
                                        greeting_id = f"{theater}_{current_year}_{current_month}_{date_num}_{time_str}_{movie_final[:10]}"

                                        if greeting_id not in [x["id"] for x in all_greetings]:
                                            event_type_str = "/".join(found_events)
                                            print(f"    - [{event_type_str}] {movie_final} {time_str}")
                                            g = {
                                                "movie": movie_final,
                                                "theater": f"CGV {theater}",
                                                "date": date_str,
                                                "time": time_str,
                                                "hall": "",
                                                "event_type": event_type_str,
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
                        page.wait_for_timeout(2000)

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
