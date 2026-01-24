#!/usr/bin/env python3
"""
CGV 무대인사 모니터링 (GitHub Actions용)
"""

import json
import os
import re
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = "stage_greetings.json"
CGV_URL = "https://cgv.co.kr/cnm/movieBook"
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
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook URL not set")
        return

    fields = [
        {"name": "영화", "value": greeting['movie'], "inline": False},
        {"name": "📍 극장", "value": greeting.get("theater", "미정"), "inline": True},
        {"name": "🎥 상영관", "value": greeting.get("hall", "미정") or "미정", "inline": True},
        {"name": "\u200b", "value": "\u200b", "inline": True},
        {"name": "📅 날짜", "value": greeting.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": greeting.get("time", "미정"), "inline": True},
        {"name": "\u200b", "value": "\u200b", "inline": True},
    ]

    embed = {
        "embeds": [{
            "title": "🎬 새로운 무대인사가 등록되었습니다!",
            "url": CGV_URL,
            "color": 5814783,
            "fields": fields,
            "footer": {"text": "CGV 무대인사 알림"},
            "timestamp": datetime.utcnow().isoformat()
        }]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            print(f"알림 전송: {greeting['movie']} - {greeting['theater']} {greeting['time']}")
    except Exception as e:
        print(f"Discord 오류: {e}")


def check_stage_greetings():
    """CGV에서 주말 무대인사 정보 수집"""
    all_greetings = []

    try:
        with sync_playwright() as p:
            # GitHub Actions에서는 headless 사용
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

            # CGV 메인 페이지
            print("CGV 접속 중...")
            page.goto(CGV_URL, timeout=60000)
            page.wait_for_timeout(8000)

            # Cloudflare 체크
            if "Cloudflare" in page.title() or "Attention" in page.title():
                print("Cloudflare 차단됨 - 우회 시도...")
                page.wait_for_timeout(5000)
                page.reload()
                page.wait_for_timeout(10000)

            print(f"페이지 제목: {page.title()}")

            # 영화 목록 가져오기
            movie_imgs = page.query_selector_all("img[alt]")
            movies = []
            for img in movie_imgs:
                alt = img.get_attribute("alt") or ""
                if alt and alt not in ["CGV", ""] and len(alt) > 1:
                    movies.append(alt)
            movies = list(dict.fromkeys(movies))[:8]
            print(f"영화 {len(movies)}개 발견")

            for movie_name in movies:
                try:
                    print(f"\n[{movie_name}] 확인 중...")

                    page.goto(CGV_URL, timeout=30000)
                    page.wait_for_timeout(5000)

                    movie_img = page.query_selector(f"img[alt='{movie_name}']")
                    if not movie_img:
                        continue
                    movie_img.click(force=True)
                    page.wait_for_timeout(4000)

                    for region in TARGET_REGIONS:
                        try:
                            # 극장 선택 팝업 열기
                            try:
                                page.click("text=선택 된 극장이 없습니다", force=True, timeout=3000)
                            except:
                                try:
                                    page.click("text=자주가는 CGV", force=True, timeout=2000)
                                except:
                                    pass
                            page.wait_for_timeout(2000)

                            # 지역 클릭
                            try:
                                page.click(f"text=/{region}\\(\\d+\\)/", force=True, timeout=3000)
                            except:
                                page.click(f"text={region}", force=True, timeout=3000)
                            page.wait_for_timeout(2000)
                            print(f"  {region} 지역 선택")

                            # 극장 목록 - 팝업 내 li 요소에서 추출
                            theater_items = page.query_selector_all("li")
                            theaters = []
                            exclude_words = ["전체", "특별관", "지역", "서울", "경기", "인천", "강원",
                                           "대전", "대구", "부산", "경상", "광주", "충청", "전라", "제주",
                                           "바로가기", "씨네톡", "예매", "매점", "메뉴", "로그인", "회원",
                                           "영화", "이벤트", "스토어", "포토", "예약", "고객센터",
                                           "더보기", "찾기", "닫기", "확인", "취소", "본문", "주요"]
                            for li in theater_items:
                                try:
                                    li_text = li.inner_text().strip()
                                    # 단일 줄이고, 2-15자 사이인 극장명
                                    if '\n' not in li_text and 2 <= len(li_text) <= 15:
                                        if not any(x in li_text for x in exclude_words):
                                            if not re.search(r'\(\d+\)', li_text):
                                                theaters.append(li_text)
                                except:
                                    continue
                            theaters = list(dict.fromkeys(theaters))[:20]  # 더 많은 극장 확인

                            for theater in theaters:
                                try:
                                    page.click(f"text={theater}", force=True, timeout=2000)
                                    page.wait_for_timeout(3000)
                                    print(f"    {theater} 극장")

                                    # 주말 날짜 확인 (버튼 텍스트가 "일\n25" 또는 "토\n26" 형태)
                                    buttons = page.query_selector_all("button")
                                    for btn in buttons:
                                        try:
                                            btn_text = btn.inner_text()  # strip 하지 않고 원본 유지
                                            # 토 또는 일이 포함되고 숫자가 있는 버튼
                                            has_weekend = "토" in btn_text or "일" in btn_text
                                            date_match = re.search(r'(\d{1,2})', btn_text)
                                            if has_weekend and date_match:
                                                date_num = date_match
                                                    btn.click(force=True)
                                                    page.wait_for_timeout(2500)

                                                    body_text = page.inner_text("body")
                                                    if "무대인사" in body_text:
                                                        body_lines = body_text.split('\n')
                                                        hall = ""
                                                        for i, line in enumerate(body_lines):
                                                            line = line.strip()
                                                            if re.search(r'\d+관|IMAX|Laser', line):
                                                                hall = line[:30]
                                                            if line == "무대인사":
                                                                for j in range(max(0, i-6), i):
                                                                    time_m = re.search(r'(\d{1,2}:\d{2})', body_lines[j])
                                                                    if time_m:
                                                                        day = "토" if "토" in btn_text else "일"
                                                                        month = datetime.now().month
                                                                        g = {
                                                                            "movie": movie_name,
                                                                            "theater": f"CGV {theater}",
                                                                            "date": f"{month}월 {date_num.group(1)}일 ({day})",
                                                                            "time": time_m.group(1),
                                                                            "hall": hall,
                                                                            "id": f"{movie_name}_{theater}_{date_num.group(1)}_{time_m.group(1)}"
                                                                        }
                                                                        if g["id"] not in [x["id"] for x in all_greetings]:
                                                                            all_greetings.append(g)
                                                                            print(f"      ★ 무대인사: {g['date']} {g['time']}")
                                                                        break
                                        except:
                                            continue
                                except:
                                    continue

                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1000)

                        except Exception as e:
                            page.keyboard.press("Escape")
                            continue

                except Exception as e:
                    print(f"  오류: {e}")
                    continue

            browser.close()

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
