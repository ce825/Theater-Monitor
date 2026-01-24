#!/Users/cehwang/miniconda3/bin/python3
"""
CGV 무대인사 모니터링 스크립트
주말(토/일) 무대인사 상영이 새로 등록되면 Discord로 알림
"""

import json
import os
import re
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# 설정
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1464577763527889137/crrzuov6ADoIoNcrJ5-jCK723zkXmjaKovNOL5WprbGlTVDjrhIKIJJcvr0RpkqDeOkx"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage_greetings.json")
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 지역 (서울/경기/인천)
TARGET_REGIONS = ["서울", "경기", "인천"]


def load_saved_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"greetings": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_discord_notification(greeting):
    embed = {
        "embeds": [{
            "title": "🎬 CGV 무대인사 발견!",
            "description": f"**{greeting['movie']}**",
            "url": CGV_URL,
            "color": 0xFF5733,
            "fields": [
                {"name": "극장", "value": greeting.get("theater", "미정"), "inline": True},
                {"name": "날짜", "value": greeting.get("date", "미정"), "inline": True},
                {"name": "시간", "value": greeting.get("time", "미정"), "inline": True},
            ],
            "footer": {"text": "CGV 무대인사"},
            "timestamp": datetime.utcnow().isoformat()
        }]
    }

    if greeting.get("hall"):
        embed["embeds"][0]["fields"].append({
            "name": "상영관", "value": greeting["hall"], "inline": True
        })

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            print(f"  알림 전송: {greeting['movie']} - {greeting['theater']} {greeting['date']} {greeting['time']}")
    except Exception as e:
        print(f"  Discord 오류: {e}")


def check_stage_greetings():
    """CGV에서 주말 무대인사 정보 수집"""
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

            # 영화 목록 가져오기
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

                    # 메인 페이지로
                    page.goto(CGV_URL, timeout=30000)
                    page.wait_for_timeout(5000)

                    # 영화 클릭
                    movie_img = page.query_selector(f"img[alt='{movie_name}']")
                    if not movie_img:
                        continue
                    movie_img.click(force=True)
                    page.wait_for_timeout(4000)

                    # 각 지역 확인
                    for region in TARGET_REGIONS:
                        try:
                            # "선택 된 극장이 없습니다" 또는 극장 선택 영역 클릭하여 팝업 열기
                            try:
                                page.click("text=선택 된 극장이 없습니다", force=True, timeout=3000)
                            except:
                                try:
                                    page.click("text=극장을 선택해 주세요", force=True, timeout=2000)
                                except:
                                    try:
                                        page.click("text=자주가는 CGV", force=True, timeout=2000)
                                    except:
                                        pass
                            page.wait_for_timeout(2000)

                            # 지역 클릭 (서울(29) 형태)
                            region_selector = f"text=/{region}\\(\\d+\\)/"
                            try:
                                page.click(region_selector, force=True, timeout=3000)
                            except:
                                page.click(f"text={region}", force=True, timeout=3000)
                            page.wait_for_timeout(2000)
                            print(f"    {region} 지역 선택")

                            # 극장 목록 가져오기 (팝업 내 극장들)
                            text = page.inner_text("body")
                            lines = text.split('\n')

                            # 극장명 추출 (강남, 강변, 건대입구 등)
                            theaters = []
                            for line in lines:
                                line = line.strip()
                                if (2 <= len(line) <= 10 and
                                    not any(x in line for x in ["전체", "특별관", "지역", "서울", "경기", "인천", "강원", "대전", "대구", "부산", "경상", "광주"]) and
                                    not re.search(r'\(\d+\)', line) and
                                    not line.isdigit()):
                                    theaters.append(line)

                            theaters = list(dict.fromkeys(theaters))[:10]

                            for theater in theaters:
                                try:
                                    # 극장 클릭
                                    page.click(f"text={theater}", force=True, timeout=2000)
                                    page.wait_for_timeout(3000)
                                    print(f"      {theater} 극장 선택")

                                    # 주말(토/일) 날짜 찾아서 클릭
                                    buttons = page.query_selector_all("button")
                                    for btn in buttons:
                                        try:
                                            btn_text = btn.inner_text().strip()
                                            # 토 또는 일이 포함된 날짜 버튼
                                            if ("토" in btn_text or "일" in btn_text) and re.search(r'\d{1,2}', btn_text):
                                                date_num = re.search(r'(\d{1,2})', btn_text)
                                                if date_num:
                                                    btn.click(force=True)
                                                    page.wait_for_timeout(2500)

                                                    # 무대인사 확인
                                                    body_text = page.inner_text("body")
                                                    if "무대인사" in body_text:
                                                        body_lines = body_text.split('\n')
                                                        current_hall = ""

                                                        for i, line in enumerate(body_lines):
                                                            line = line.strip()

                                                            # 상영관 정보
                                                            if re.search(r'\d+관|IMAX|Laser', line):
                                                                current_hall = line[:30]

                                                            # 무대인사 발견
                                                            if line == "무대인사":
                                                                for j in range(max(0, i-6), i):
                                                                    time_match = re.search(r'(\d{1,2}:\d{2})', body_lines[j])
                                                                    if time_match:
                                                                        day_type = "토" if "토" in btn_text else "일"
                                                                        date_str = f"{date_num.group(1)}일({day_type})"

                                                                        greeting = {
                                                                            "movie": movie_name,
                                                                            "theater": f"CGV {theater}",
                                                                            "date": date_str,
                                                                            "time": time_match.group(1),
                                                                            "hall": current_hall,
                                                                            "id": f"{movie_name}_{theater}_{date_num.group(1)}_{time_match.group(1)}"
                                                                        }

                                                                        if greeting["id"] not in [g["id"] for g in all_greetings]:
                                                                            all_greetings.append(greeting)
                                                                            print(f"        ★ 무대인사: {date_str} {time_match.group(1)}")
                                                                        break
                                        except:
                                            continue

                                except Exception as e:
                                    continue

                            # 팝업 닫기 (X 버튼 또는 ESC)
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)

                        except Exception as e:
                            print(f"    {region} 오류: {e}")
                            page.keyboard.press("Escape")
                            continue

                except Exception as e:
                    print(f"    오류: {e}")
                    continue

            browser.close()

    except Exception as e:
        print(f"[{datetime.now()}] 브라우저 오류: {e}")
        return None

    return all_greetings


def main():
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

    # 새 무대인사 확인
    new_greetings = [g for g in greetings if g.get("id") and g["id"] not in saved_ids]

    if new_greetings:
        print(f"[{datetime.now()}] 새 무대인사 {len(new_greetings)}개!")
        for g in new_greetings:
            send_discord_notification(g)

        saved_data["greetings"].extend(new_greetings)
        save_data(saved_data)
    else:
        print(f"[{datetime.now()}] 새 무대인사 없음")


if __name__ == "__main__":
    main()
