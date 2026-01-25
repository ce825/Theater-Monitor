#!/usr/bin/env python3
"""
CGV 무대인사 모니터링 (GitHub Actions용)
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
    ("서울", "강남"),
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

    fields = [
        {"name": "🎬 영화", "value": greeting.get("movie", "미정"), "inline": False},
        {"name": "📍 극장", "value": greeting.get("theater", "미정"), "inline": True},
        {"name": "📅 날짜", "value": greeting.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": greeting.get("time", "미정"), "inline": True},
    ]
    if greeting.get("hall"):
        fields.append({"name": "🎥 상영관", "value": greeting["hall"], "inline": True})

    embed = {
        "embeds": [{
            "title": "🎬 새로운 무대인사가 등록되었습니다!",
            "url": CGV_URL,
            "color": 5814783,
            "fields": fields,
            "footer": {"text": "CGV 무대인사 알림"},
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
    """CGV 타겟 극장들의 주말 무대인사 확인"""
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

                    # 6. 주말 날짜 클릭 (토, 일)
                    for day in ["토", "일"]:
                        try:
                            js_code = """() => {
                                const day = '%s';
                                const elements = document.querySelectorAll('button, li, a, div, span');
                                for (const el of elements) {
                                    const text = (el.innerText || '').trim();
                                    const pattern = new RegExp('^' + day + '[\\\\s\\\\n]+\\\\d');
                                    if (pattern.test(text)) {
                                        el.click();
                                        return text;
                                    }
                                }
                                return false;
                            }""" % day
                            clicked = page.evaluate(js_code)
                            if not clicked:
                                continue
                            page.wait_for_timeout(2500)

                            # 7. 무대인사 확인
                            body = page.inner_text("body")
                            if "무대인사" in body:
                                print(f"  ★ {day}요일 무대인사 발견!")
                                today = datetime.now()
                                day_offset = 0 if day == "토" else 1
                                days_until_sat = (5 - today.weekday()) % 7
                                if today.weekday() == 5:
                                    days_until_sat = 0
                                elif today.weekday() == 6:
                                    days_until_sat = 6

                                target_date = today + timedelta(days=days_until_sat + day_offset)
                                date_str = f"{target_date.month}월 {target_date.day}일 ({day})"

                                # 시간 및 영화 제목 추출
                                lines = body.split('\n')
                                exclude_words = ["무대인사", "GV", "전체", "오전", "오후", "18시 이후", "심야", theater, "예매", "상영시간표"]
                                hall_patterns = r'(DOLBY|ATMOS|SCREENX|SOUNDX|4DX|IMAX|SPHERE|Laser|리클라이너|아트하우스|\d+관|2D|3D|전도연관|씨네앤포레|씨네\&포레|CINE|MX관|GOLD CLASS|SUITE CINEMA|PREMIUM|TEMPUR|STARIUM|CGV|특별관|일반|조조)'

                                # 페이지에서 영화 제목 후보들을 수집
                                movie_candidates = []
                                for idx, line in enumerate(lines):
                                    text = line.strip()
                                    if len(text) >= 2 and re.search(r'[가-힣]', text):
                                        if not re.match(r'^[\d:~\-\(\)\[\]관]', text):
                                            if text not in exclude_words:
                                                if not re.search(r'(석|좌석|잔여|매진|마감|\d+:\d+|~|개봉)', text):
                                                    if not re.search(hall_patterns, text, re.IGNORECASE):
                                                        movie_candidates.append((idx, text))

                                for i, line in enumerate(lines):
                                    line_stripped = line.strip()
                                    if line_stripped == "무대인사":
                                        for j in range(max(0, i-5), i):
                                            tm = re.search(r'(\d{1,2}:\d{2})', lines[j])
                                            if tm:
                                                time_str = tm.group(1)
                                                movie_name = ""

                                                # 방법 1: 무대인사 위로 올라가며 영화 제목 찾기
                                                for k in range(i-1, max(0, i-30), -1):
                                                    candidate = lines[k].strip()
                                                    if len(candidate) >= 2 and re.search(r'[가-힣]', candidate):
                                                        if not re.match(r'^[\d:~\-\(\)\[\]관]', candidate):
                                                            if candidate not in exclude_words:
                                                                if not re.search(r'(석|좌석|잔여|매진|마감|\d+:\d+|~|개봉)', candidate):
                                                                    if not re.search(hall_patterns, candidate, re.IGNORECASE):
                                                                        movie_name = candidate
                                                                        break

                                                # 방법 2: 못 찾으면 가장 가까운 영화 제목 후보 사용
                                                if not movie_name and movie_candidates:
                                                    closest = min(movie_candidates, key=lambda x: abs(x[0] - i))
                                                    if abs(closest[0] - i) < 50:
                                                        movie_name = closest[1]

                                                print(f"    - {movie_name} {time_str}")
                                                g = {
                                                    "movie": movie_name if movie_name else "무대인사",
                                                    "theater": f"CGV {theater}",
                                                    "date": date_str,
                                                    "time": time_str,
                                                    "hall": "",
                                                    "id": f"{theater}_{target_date.month}_{target_date.day}_{time_str}"
                                                }
                                                if g["id"] not in [x["id"] for x in all_greetings]:
                                                    all_greetings.append(g)
                            else:
                                print(f"  {day}요일 무대인사 없음")
                        except Exception as e:
                            print(f"  {day}요일 오류: {e}")

                except Exception as e:
                    print(f"  [{theater}] 오류: {e}")
                    continue

            browser.close()
            print("\n" + "="*50)
            print("모든 극장 확인 완료!")

    except Exception as e:
        print(f"브라우저 오류: {e}")
        return None

    return all_greetings


def main():
    print(f"[{datetime.now()}] CGV 무대인사 모니터링 시작...")

    saved_data = load_saved_data()
    saved_ids = set(g.get("id", "") for g in saved_data.get("greetings", []))

    greetings = check_stage_greetings()

    if greetings is None:
        print("조회 실패")
        return

    print(f"\n총 {len(greetings)}개 무대인사 발견")

    if not saved_data.get("greetings"):
        print("첫 실행 - 저장")
        saved_data["greetings"] = greetings
        save_data(saved_data)
        if greetings and DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json={
                "content": f"✅ CGV 무대인사 모니터링 시작!\n{len(greetings)}개 무대인사 추적 중"
            }, timeout=10)
        return

    new_greetings = [g for g in greetings if g.get("id") and g["id"] not in saved_ids]

    if new_greetings:
        print(f"새 무대인사 {len(new_greetings)}개!")
        for g in new_greetings:
            send_discord_notification(g)
        saved_data["greetings"].extend(new_greetings)
        save_data(saved_data)
    else:
        print("새 무대인사 없음")


if __name__ == "__main__":
    main()
