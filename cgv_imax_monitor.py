#!/usr/bin/env python3
"""
CGV IMAX 예매 오픈 모니터링 (GitHub Actions용)
특정 영화의 IMAX 상영 스케줄이 새로 등록되면 Discord로 알림
"""

import json
import os
import random
import time
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1485212944504721499/8h9YOMMJ9dsMgGKPzHfpdWjeoa0H0GCoA0XiLkr-ghlk6piRz-a3cdu8C0iC0FjU3u8Z")
DATA_FILE = "imax_showings.json"
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 모니터링 설정
TARGET_THEATER = ("서울", "용산아이파크몰")
TARGET_MOVIE = "헤일메리"  # 부분 매칭 (제목 변형 대응)


def load_saved_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"showings": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_discord_notification(showing, notification_type="new"):
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook URL not set")
        return

    if notification_type == "preparing":
        title = "⏳ IMAX 예매 준비중!"
        color = 0xFFA500
    elif notification_type == "sales_started":
        title = "🎟️ IMAX 예매 오픈!"
        color = 0x00FF00
    elif notification_type == "reopened":
        title = "🔄 IMAX 취소표 발생!"
        color = 0x9932CC
    else:
        title = "🆕 IMAX 상영 일정 등록!"
        color = 0x0066FF

    fields = [
        {"name": "🎬 영화", "value": showing.get("movie", "미정"), "inline": False},
        {"name": "🎥 상영관", "value": showing.get("hall", "IMAX"), "inline": True},
        {"name": "📍 극장", "value": showing.get("theater", "미정"), "inline": True},
        {"name": "📅 날짜", "value": showing.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": showing.get("time", "미정"), "inline": True},
    ]

    embed = {
        "embeds": [{
            "title": title,
            "url": CGV_URL,
            "color": color,
            "fields": fields,
            "footer": {"text": "CGV IMAX 예매 알림"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            status_msg = {"preparing": "예매준비중", "sales_started": "예매오픈", "reopened": "취소표", "new": "신규"}
            print(f"  알림 전송 [{status_msg.get(notification_type, 'new')}]: {showing['movie']} {showing['date']} {showing['time']} {showing.get('hall', '')}")
    except Exception as e:
        print(f"  Discord 오류: {e}")


def check_imax_showings():
    """용산아이파크몰의 프로젝트 헤일메리 IMAX 상영 확인"""
    all_showings = []
    region, theater = TARGET_THEATER

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

            # 1. CGV 예매 페이지 이동
            page.goto(CGV_URL, timeout=60000)
            page.wait_for_timeout(3000)

            if "Cloudflare" in page.title() or "Attention" in page.title():
                print("  Cloudflare 감지 - 대기 중...")
                page.wait_for_timeout(10000)

            page.wait_for_selector("text=극장을 선택해 주세요", timeout=10000)
            page.wait_for_timeout(500)

            # 2. 극장 선택
            page.click("text=극장을 선택해 주세요", timeout=5000)
            page.wait_for_timeout(800)

            try:
                page.wait_for_selector(".loading_pageContainer__fvLY_", state="hidden", timeout=5000)
            except:
                pass

            page.click(f"text=/{region}\\(\\d+\\)/", timeout=5000)
            page.wait_for_timeout(500)
            page.click(f"text={theater}", timeout=5000)
            page.wait_for_timeout(500)

            page.evaluate('''() => {
                const elements = document.querySelectorAll('button, a, div, span');
                for (const el of elements) {
                    if ((el.innerText || '').trim() === '극장선택') { el.click(); return true; }
                }
                return false;
            }''')
            page.wait_for_timeout(1500)
            print(f"  극장 선택 완료: {theater}")

            # 3. 모든 날짜 확인
            checked_dates = set()
            max_arrow_clicks = 10
            arrow_clicks = 0

            while arrow_clicks <= max_arrow_clicks:
                # 날짜 버튼 추출
                all_dates = page.evaluate(r"""() => {
                    var results = [];
                    var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                    for (var i = 0; i < buttons.length; i++) {
                        var btn = buttons[i];
                        var spans = btn.querySelectorAll('span');
                        if (spans.length < 2) continue;
                        var dayText = spans[0].innerText.trim();
                        var dateText = spans[1].innerText.trim();
                        if (/^(월|화|수|목|금|토|일|오늘)$/.test(dayText) && /^\d{1,2}$/.test(dateText)) {
                            var isDisabled = btn.disabled || btn.className.indexOf('Disabled') !== -1 || btn.className.indexOf('disabled') !== -1;
                            results.push({day: dayText, date: dateText.replace(/^0/, '') || '0', disabled: isDisabled});
                        }
                    }
                    return results;
                }""")

                # 중복 제거
                seen = set()
                unique_dates = []
                for d in all_dates:
                    key = f"{d['day']}_{d['date']}"
                    if key not in seen:
                        seen.add(key)
                        unique_dates.append(d)
                dates_to_check = sorted(unique_dates, key=lambda x: int(x['date']))

                found_dates = [f"{d['day']}{d['date']}" for d in dates_to_check]
                print(f"  발견된 날짜: {found_dates}")

                new_dates = [d for d in dates_to_check if f"{d['day']}_{d['date']}" not in checked_dates]
                if not new_dates:
                    if arrow_clicks == 0 and not dates_to_check:
                        pass
                    else:
                        print(f"  더 이상 새로운 날짜 없음")
                        break

                for date_info in new_dates:
                    day = date_info["day"]
                    date_num = date_info["date"]
                    checked_dates.add(f"{day}_{date_num}")

                    if date_info.get("disabled"):
                        continue

                    try:
                        # 날짜 클릭
                        click_result = page.evaluate(
                            r"""(args) => {
                            var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                            for (var i = 0; i < buttons.length; i++) {
                                var btn = buttons[i];
                                var spans = btn.querySelectorAll('span');
                                if (spans.length < 2) continue;
                                var dayText = spans[0].innerText.trim();
                                var dateText = spans[1].innerText.trim().replace(/^0/, '');
                                if (dayText === args.day && dateText === args.dateNum) {
                                    if (btn.disabled || btn.className.indexOf('Disabled') !== -1) {
                                        return {clicked: false, disabled: true};
                                    }
                                    btn.scrollIntoView({behavior: 'instant', inline: 'center'});
                                    btn.click();
                                    return {clicked: true};
                                }
                            }
                            return {clicked: false, notFound: true};
                        }""", {"day": day, "dateNum": date_num})

                        if not click_result.get("clicked"):
                            continue

                        page.wait_for_timeout(1500)

                        # 선택 확인
                        selected = page.evaluate(r"""() => {
                            var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                            for (var i = 0; i < buttons.length; i++) {
                                if (buttons[i].className.indexOf('Active') !== -1) {
                                    var spans = buttons[i].querySelectorAll('span');
                                    if (spans.length >= 2) {
                                        return {day: spans[0].innerText.trim(), date: spans[1].innerText.trim().replace(/^0/, '')};
                                    }
                                }
                            }
                            return null;
                        }""")

                        if selected and selected.get('date') != date_num:
                            continue

                        # 스크롤로 영화 로드
                        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
                        page.wait_for_timeout(600)
                        page.evaluate("() => { window.scrollTo(0, 0); }")
                        page.wait_for_timeout(400)

                        # 헤일메리 IMAX 상영 추출
                        imax_events = page.evaluate(r"""(targetMovie) => {
                            var results = [];
                            var bodyText = document.body.innerText || '';

                            // 타겟 영화가 페이지에 없으면 빠르게 스킵
                            if (bodyText.indexOf(targetMovie) === -1) return results;

                            // IMAX도 없으면 스킵
                            if (bodyText.indexOf('IMAX') === -1) return results;

                            // 스케줄 영역의 모든 블록 탐색
                            var blocks = document.querySelectorAll('[class*="time"], [class*="schedule"], [class*="showtime"], li, div');
                            for (var i = 0; i < blocks.length; i++) {
                                var block = blocks[i];
                                var rect = block.getBoundingClientRect();
                                if (rect.top < 400) continue;

                                var blockText = block.innerText || '';

                                // 시간 패턴 확인
                                var timeMatch = blockText.match(/(\d{1,2}:\d{2})/);
                                if (!timeMatch) continue;

                                // IMAX 포함 확인
                                if (blockText.indexOf('IMAX') === -1) continue;

                                var timeStr = timeMatch[1];
                                var isPreparing = blockText.indexOf('예매 준비중') !== -1 || blockText.indexOf('예매준비중') !== -1;
                                var isSoldOut = blockText.indexOf('매진') !== -1 || blockText.indexOf('마감') !== -1;

                                // 상영관 정보 추출
                                var hallMatch = blockText.match(/(IMAX[^\n]*)/);
                                var hall = hallMatch ? hallMatch[1].trim() : 'IMAX';

                                // 영화 제목 확인 - 부모 요소에서 타겟 영화 포함 여부
                                var found = false;
                                var parent = block;
                                for (var p = 0; p < 10 && parent; p++) {
                                    var parentText = parent.innerText || '';
                                    if (parentText.indexOf(targetMovie) !== -1) {
                                        found = true;
                                        break;
                                    }
                                    parent = parent.parentElement;
                                }

                                if (!found) continue;

                                // 영화 제목 추출
                                var movieName = '';
                                var searchParent = block;
                                for (var p = 0; p < 10 && searchParent; p++) {
                                    var pText = searchParent.innerText || '';
                                    var lines = pText.split('\n');
                                    for (var l = 0; l < lines.length; l++) {
                                        var line = lines[l].trim();
                                        if (line.indexOf(targetMovie) !== -1 && line.length >= 2 && line.length <= 50) {
                                            movieName = line;
                                            break;
                                        }
                                    }
                                    if (movieName) break;
                                    searchParent = searchParent.parentElement;
                                }

                                if (!movieName) movieName = targetMovie;

                                // 중복 체크
                                var isDup = false;
                                for (var r = 0; r < results.length; r++) {
                                    if (results[r].time === timeStr && results[r].hall === hall) {
                                        isDup = true;
                                        break;
                                    }
                                }
                                if (!isDup) {
                                    results.push({
                                        movie: movieName,
                                        time: timeStr,
                                        hall: hall,
                                        preparing: isPreparing,
                                        soldOut: isSoldOut
                                    });
                                }
                            }
                            return results;
                        }""", TARGET_MOVIE)

                        if imax_events:
                            print(f"  ★ {day} {date_num}일 IMAX 발견: {len(imax_events)}건")

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

                            for event in imax_events:
                                time_str = event.get("time", "")
                                hall = event.get("hall", "IMAX")
                                showing_id = f"용산_{current_year}_{current_month}_{date_num}_{time_str}_{hall}"

                                if showing_id not in [x["id"] for x in all_showings]:
                                    status_str = " [예매준비중]" if event.get("preparing") else (" [매진]" if event.get("soldOut") else "")
                                    print(f"    - {event['movie']} {time_str} {hall}{status_str}")
                                    all_showings.append({
                                        "movie": event["movie"],
                                        "theater": f"CGV {theater}",
                                        "date": date_str,
                                        "time": time_str,
                                        "hall": hall,
                                        "id": showing_id,
                                        "preparing": event.get("preparing", False),
                                        "sold_out": event.get("soldOut", False)
                                    })

                    except Exception as e:
                        print(f"  {day} {date_num}일 오류: {e}")

                # 다음 날짜로 이동
                arrow_clicked = page.evaluate(r"""() => {
                    var nextBtn = document.querySelector('.swiper-button-next');
                    if (nextBtn && !nextBtn.classList.contains('swiper-button-disabled')) {
                        nextBtn.click();
                        return true;
                    }
                    return false;
                }""")

                if not arrow_clicked:
                    print(f"  더 이상 날짜 없음")
                    break

                arrow_clicks += 1
                page.wait_for_timeout(800)

            browser.close()
            print("\n확인 완료!")

    except Exception as e:
        print(f"브라우저 오류: {e}")
        return None

    return all_showings


def main():
    delay = random.randint(0, 30)
    print(f"[{datetime.now()}] 랜덤 딜레이: {delay}초")
    time.sleep(delay)

    print(f"[{datetime.now()}] CGV IMAX 모니터링 시작 - {TARGET_MOVIE}...")

    saved_data = load_saved_data()
    saved_ids = set(s.get("id", "") for s in saved_data.get("showings", []))

    showings = check_imax_showings()

    if showings is None:
        print("조회 실패")
        return

    print(f"\n총 {len(showings)}개 IMAX 상영 발견")

    # 첫 실행
    if not saved_data.get("showings"):
        print("첫 실행 - 저장")
        saved_data["showings"] = showings
        save_data(saved_data)
        if showings and DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json={
                "content": f"✅ CGV IMAX 모니터링 시작!\n{TARGET_MOVIE} IMAX 상영 {len(showings)}개 추적 중"
            }, timeout=10)
        return

    # 기존 상영을 ID로 매핑
    saved_by_id = {s.get("id"): s for s in saved_data.get("showings", []) if s.get("id")}

    new_showings = []
    sales_started = []
    reopened = []

    for s in showings:
        sid = s.get("id")
        if not sid:
            continue

        if sid not in saved_ids:
            new_showings.append(s)
        else:
            old = saved_by_id.get(sid)
            if old:
                if old.get("preparing") and not s.get("preparing"):
                    sales_started.append(s)
                    old["preparing"] = False
                if old.get("sold_out") and not s.get("sold_out"):
                    reopened.append(s)
                    old["sold_out"] = False
                if not old.get("sold_out") and s.get("sold_out"):
                    old["sold_out"] = True

    # 알림 전송
    if new_showings:
        print(f"새 IMAX 상영 {len(new_showings)}개!")
        for s in new_showings:
            if s.get("preparing"):
                send_discord_notification(s, "preparing")
            else:
                send_discord_notification(s, "new")
        saved_data["showings"].extend(new_showings)

    if sales_started:
        print(f"예매 오픈 {len(sales_started)}개!")
        for s in sales_started:
            send_discord_notification(s, "sales_started")

    if reopened:
        print(f"취소표 발생 {len(reopened)}개!")
        for s in reopened:
            send_discord_notification(s, "reopened")

    if new_showings or sales_started or reopened:
        save_data(saved_data)
    else:
        print("새 IMAX 상영 없음")


if __name__ == "__main__":
    main()
