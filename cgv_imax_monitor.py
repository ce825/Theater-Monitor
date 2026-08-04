#!/usr/bin/env python3
"""
CGV IMAX/4DX 스케줄 모니터링 (GitHub Actions용)
용산아이파크몰의 모든 영화 IMAX/4DX 상영이 새로 등록되면 Discord로 알림
"""

import json
import os
import random
import time
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# 웹훅은 시크릿/환경변수로만 받는다 (IMAX_DISCORD_WEBHOOK_URL -> DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = "imax_showings.json"
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

TARGET_THEATER = ("서울", "용산아이파크몰")
# IMAX, 4DX 섹션 감지 키워드
TARGET_HALLS = ["IMAX", "4DX"]


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

    hall_type = showing.get("hall", "")
    is_imax = "IMAX" in hall_type
    hall_label = "IMAX" if is_imax else "4DX"

    if notification_type == "preparing":
        title = f"⏳ {hall_label} 예매 준비중!"
        color = 0xFFA500
    elif notification_type == "sales_started":
        title = f"🎟️ {hall_label} 예매 오픈!"
        color = 0x00FF00
    elif notification_type == "reopened":
        title = f"🔄 {hall_label} 취소표 발생!"
        color = 0x9932CC
    else:
        title = f"🆕 {hall_label} 상영 일정 등록!"
        color = 0x0066FF if is_imax else 0xFF4500

    fields = [
        {"name": "🎬 영화", "value": showing.get("movie", "미정"), "inline": False},
        {"name": "🎥 상영관", "value": hall_type, "inline": True},
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
            "footer": {"text": f"CGV {hall_label} 예매 알림"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        if response.status_code == 204:
            status_msg = {"preparing": "예매준비중", "sales_started": "예매오픈", "reopened": "취소표", "new": "신규"}
            print(f"  알림 전송 [{status_msg.get(notification_type, 'new')}]: {showing['movie']} {showing['date']} {showing['time']} {hall_type}")
        time.sleep(0.5)
    except Exception as e:
        print(f"  Discord 오류: {e}")


def check_special_showings():
    """용산아이파크몰의 모든 영화 IMAX/4DX 상영 확인"""
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

                        # 스케줄 로딩 대기
                        page.evaluate(r"""() => {
                            return new Promise(function(resolve) {
                                var attempts = 0;
                                var check = function() {
                                    attempts++;
                                    var loading = document.querySelector('[class*="loading"]');
                                    if (loading && loading.offsetHeight > 0 && attempts < 20) {
                                        setTimeout(check, 200);
                                        return;
                                    }
                                    resolve();
                                };
                                setTimeout(check, 500);
                            });
                        }""")
                        page.wait_for_timeout(2000)

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

                        # 모든 영화의 IMAX/4DX 상영 추출
                        events = page.evaluate(r"""() => {
                            var results = [];
                            var bodyText = document.body.innerText || '';

                            // IMAX 또는 4DX가 페이지에 없으면 스킵
                            if (bodyText.indexOf('IMAX') === -1 && bodyText.indexOf('4DX') === -1) return results;

                            var lines = bodyText.split('\n');
                            var currentMovie = '';
                            var currentHall = '';
                            var inSpecialHall = false;

                            for (var i = 0; i < lines.length; i++) {
                                var line = lines[i].trim();
                                if (!line) continue;

                                // 1. IMAX/4DX 섹션 감지 (영화 제목 감지보다 먼저!)
                                if (currentMovie && (line.indexOf('IMAX') !== -1 || line.indexOf('4DX') !== -1)) {
                                    inSpecialHall = true;
                                    currentHall = line;
                                    continue;
                                }

                                // 2. 다른 상영관 섹션으로 넘어가면 종료
                                if (inSpecialHall && (/^2D$/.test(line) || /^3D$/.test(line) ||
                                    /^\d+관/.test(line) || line.indexOf('SCREENX') !== -1 ||
                                    line.indexOf('DOLBY') !== -1 || line.indexOf('리클라이너') !== -1 ||
                                    line.indexOf('스트레스리스') !== -1 || line.indexOf('템퍼') !== -1 ||
                                    line.indexOf('골드클래스') !== -1 || line.indexOf('PREMIUM') !== -1 ||
                                    line.indexOf('PRIVATE') !== -1)) {
                                    inSpecialHall = false;
                                    currentHall = '';
                                    continue;
                                }

                                // 3. 영화 제목 감지 (한글/영문 시작, 2~50자, 상영관/태그 아님)
                                if (/^[가-힣a-zA-Z]/.test(line) && line.length >= 2 && line.length <= 50 &&
                                    !/^\d/.test(line) && !/석$/.test(line) &&
                                    !/^(IMAX|4DX|ULTRA|Laser|2D|3D|일반|특별관|매진|마감|예매|예매 준비중|잔여|좌석|자막|더빙|전체|오전|오후|심야|조조|프리미어 상영|응원 상영회|응원상영)$/.test(line) &&
                                    !/(관$|석$)/.test(line) && !/^\d+관/.test(line) &&
                                    line.indexOf('시네마') === -1 && line.indexOf('CINE') === -1 &&
                                    line.indexOf('[') === -1 && line.indexOf('(') === -1 &&
                                    line.indexOf('리클라이너') === -1 && line.indexOf('골드클래스') === -1 &&
                                    line.indexOf('스트레스리스') === -1 && line.indexOf('프리미엄') === -1 &&
                                    line.indexOf('SCREENX') === -1 && line.indexOf('DOLBY') === -1 &&
                                    line.indexOf('PRIVATE') === -1 && line.indexOf('PREMIUM') === -1 &&
                                    line.indexOf('IMAX') === -1 && line.indexOf('4DX') === -1 &&
                                    line.indexOf('ULTRA') === -1 && line.indexOf('ATMOS') === -1 &&
                                    line.indexOf('Laser') === -1 &&
                                    line.indexOf('템퍼') === -1 &&
                                    line.indexOf('영등포') === -1 && line.indexOf('용산') === -1 &&
                                    line.indexOf('CGV') === -1 && line.indexOf('전체보기') === -1) {

                                    // 새 영화 시작 → 이전 섹션 리셋
                                    currentMovie = line;
                                    inSpecialHall = false;
                                    currentHall = '';
                                    continue;
                                }

                                // IMAX/4DX 섹션 내에서 시간 추출
                                if (currentMovie && inSpecialHall) {
                                    var timeMatch = line.match(/^(\d{1,2}:\d{2})/);
                                    if (timeMatch) {
                                        var timeStr = timeMatch[1];
                                        var isPreparing = false;
                                        var isSoldOut = false;

                                        for (var j = i; j < Math.min(i + 4, lines.length); j++) {
                                            var checkLine = lines[j];
                                            if (checkLine.indexOf('예매 준비중') !== -1 || checkLine.indexOf('예매준비중') !== -1) isPreparing = true;
                                            if (checkLine.indexOf('매진') !== -1 || checkLine.indexOf('마감') !== -1) isSoldOut = true;
                                        }

                                        // 중복 체크 (같은 영화+시간+상영관)
                                        var isDup = false;
                                        for (var r = 0; r < results.length; r++) {
                                            if (results[r].movie === currentMovie && results[r].time === timeStr && results[r].hall === currentHall) {
                                                isDup = true; break;
                                            }
                                        }
                                        if (!isDup) {
                                            results.push({
                                                movie: currentMovie,
                                                time: timeStr,
                                                hall: currentHall,
                                                preparing: isPreparing,
                                                soldOut: isSoldOut
                                            });
                                        }
                                    }
                                }
                            }
                            return results;
                        }""")

                        if events:
                            print(f"  ★ {day} {date_num}일 IMAX/4DX 발견: {len(events)}건")

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

                            for event in events:
                                movie = event.get("movie", "미정")
                                time_str = event.get("time", "")
                                hall = event.get("hall", "")
                                showing_id = f"용산_{current_year}_{current_month}_{date_num}_{time_str}_{movie[:10]}_{hall}"

                                if showing_id not in [x["id"] for x in all_showings]:
                                    status_str = " [예매준비중]" if event.get("preparing") else (" [매진]" if event.get("soldOut") else "")
                                    print(f"    - {movie} {time_str} {hall}{status_str}")
                                    all_showings.append({
                                        "movie": movie,
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

    print(f"[{datetime.now()}] CGV IMAX/4DX 모니터링 시작 - 용산아이파크몰...")

    saved_data = load_saved_data()
    saved_ids = set(s.get("id", "") for s in saved_data.get("showings", []))

    showings = check_special_showings()

    if showings is None:
        print("조회 실패")
        return

    print(f"\n총 {len(showings)}개 IMAX/4DX 상영 발견")

    # 첫 실행
    if not saved_data.get("showings"):
        print("첫 실행 - 저장")
        saved_data["showings"] = showings
        save_data(saved_data)
        if showings and DISCORD_WEBHOOK_URL:
            requests.post(DISCORD_WEBHOOK_URL, json={
                "content": f"✅ CGV IMAX/4DX 모니터링 시작!\n용산아이파크몰 IMAX/4DX 상영 {len(showings)}개 추적 중"
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
        print(f"새 IMAX/4DX 상영 {len(new_showings)}개!")
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
        print("새 IMAX/4DX 상영 없음")


if __name__ == "__main__":
    main()
