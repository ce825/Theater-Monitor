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
import holidays
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DATA_FILE = "stage_greetings.json"
CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 타겟 극장 리스트: (지역, 극장명)
TARGET_THEATERS = [
    ("서울", "용산아이파크몰"),
    ("서울", "영등포타임스퀘어"),
    ("서울", "왕십리"),
    ("서울", "건대입구"),
    ("서울", "강변"),
    ("서울", "여의도"),
    ("서울", "압구정"),
    ("서울", "홍대"),
]

# 한국 공휴일 (자동 계산 - 음력/대체공휴일 포함)
KR_HOLIDAYS = holidays.KR()


def is_holiday(check_date):
    """해당 날짜가 공휴일인지 확인"""
    return check_date in KR_HOLIDAYS


def get_holidays_in_range(start_date, days=30):
    """주어진 기간 내의 공휴일 날짜 목록 반환"""
    holiday_list = []
    for i in range(days):
        check_date = start_date + timedelta(days=i)
        if is_holiday(check_date):
            day_name = ["월", "화", "수", "목", "금", "토", "일"][check_date.weekday()]
            holiday_list.append({
                "month": check_date.month,
                "day": check_date.day,
                "day_name": day_name
            })
    return holiday_list


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
      - "reopened": 매진 → 예매 가능 (취소표 발생)
    """
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook URL not set")
        return

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
    elif notification_type == "reopened":
        title = f"🔄 {event_type} 취소표 발생!"
        color = 0x9932CC  # 보라색
        footer_text = f"CGV {event_type} - 매진 → 예매 가능"
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
            status_msg = {"preparing": "예매준비중", "sales_started": "예매오픈", "reopened": "취소표", "new": "신규"}
            print(f"  알림 전송 [{status_msg.get(notification_type, 'new')}]: {greeting['movie']} - {greeting['theater']} {greeting['date']} {greeting['time']}")
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

                    # 7. 모든 주말 + 공휴일 날짜 확인 (화살표 클릭으로 날짜 범위 확장)
                    checked_dates = set()
                    max_arrow_clicks = 10
                    arrow_clicks = 0

                    # 현재 월 기준 공휴일 목록 가져오기
                    current_holidays = get_holidays_in_range(datetime.now(), days=60)
                    holiday_dates = {(h["day_name"], h["day"]) for h in current_holidays}

                    while arrow_clicks <= max_arrow_clicks:
                        # JavaScript로 캘린더 버튼에서 날짜 추출 (Swiper 기반 UI)
                        all_dates = page.evaluate(r"""() => {
                            var results = [];
                            var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                            for (var i = 0; i < buttons.length; i++) {
                                var btn = buttons[i];
                                var spans = btn.querySelectorAll('span');
                                if (spans.length < 2) continue;
                                var dayText = spans[0].innerText.trim();
                                var dateText = spans[1].innerText.trim();
                                // "오늘" 등 특수 텍스트는 스킵, 요일만 매칭
                                if (/^(월|화|수|목|금|토|일)$/.test(dayText) && /^\d{1,2}$/.test(dateText)) {
                                    var isDisabled = btn.disabled || btn.className.indexOf('Disabled') !== -1 || btn.className.indexOf('disabled') !== -1;
                                    results.push({day: dayText, date: dateText.replace(/^0/, '') || '0', disabled: isDisabled});
                                }
                            }
                            return results;
                        }""")

                        # 주말 또는 공휴일만 필터링 (비활성 날짜 정보 유지)
                        weekend_dates = []
                        for d in all_dates:
                            is_weekend = d['day'] in ['토', '일']
                            is_holiday_date = (d['day'], int(d['date'])) in holiday_dates
                            if is_weekend or is_holiday_date:
                                weekend_dates.append(d)

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
                        print(f"  발견된 주말/공휴일: {found_dates}")

                        # 새로운 주말 날짜가 없으면 종료
                        new_dates = [d for d in weekend_dates if f"{d['day']}_{d['date']}" not in checked_dates]
                        if not new_dates:
                            if arrow_clicks == 0 and not weekend_dates:
                                pass
                            else:
                                print(f"  더 이상 새로운 주말/공휴일 날짜 없음 → 다음 극장")
                                break

                        # 새로운 날짜만 확인
                        for date_info in new_dates:
                            day = date_info["day"]
                            date_num = date_info["date"]
                            date_key = f"{day}_{date_num}"
                            checked_dates.add(date_key)

                            # 비활성 날짜 스킵 (날짜 추출 시 이미 확인)
                            if date_info.get("disabled"):
                                print(f"    날짜 스킵(비활성): {day} {date_num}")
                                continue

                            try:
                                # button[class*="dayScroll_scrollItem"] 내 span으로 날짜 매칭 후 버튼 클릭
                                click_result = page.evaluate(
                                    r"""(args) => {
                                    var day = args.day;
                                    var dateNum = args.dateNum;
                                    var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                                    for (var i = 0; i < buttons.length; i++) {
                                        var btn = buttons[i];
                                        var spans = btn.querySelectorAll('span');
                                        if (spans.length < 2) continue;
                                        var dayText = spans[0].innerText.trim();
                                        var dateText = spans[1].innerText.trim().replace(/^0/, '');
                                        if (dayText === day && dateText === dateNum) {
                                            if (btn.disabled || btn.className.indexOf('Disabled') !== -1 || btn.className.indexOf('disabled') !== -1) {
                                                return {clicked: false, disabled: true};
                                            }
                                            btn.scrollIntoView({behavior: 'instant', inline: 'center'});
                                            btn.click();
                                            return {clicked: true};
                                        }
                                    }
                                    return {clicked: false, notFound: true};
                                }""", {"day": day, "dateNum": date_num})

                                if click_result.get("disabled"):
                                    print(f"    날짜 스킵(비활성): {day} {date_num}")
                                    continue
                                if not click_result.get("clicked"):
                                    print(f"    날짜 스킵(미발견): {day} {date_num}")
                                    continue

                                print(f"    날짜 클릭: {day} {date_num}")

                                # 스케줄 내용이 실제로 변경될 때까지 대기
                                page.evaluate(r"""(dateNum) => {
                                    return new Promise(function(resolve) {
                                        var attempts = 0;
                                        var check = function() {
                                            attempts++;
                                            // 로딩 오버레이가 있으면 대기
                                            var loading = document.querySelector('[class*="loading"]');
                                            if (loading && loading.offsetHeight > 0 && attempts < 20) {
                                                setTimeout(check, 200);
                                                return;
                                            }
                                            if (attempts >= 20) resolve();
                                            else resolve();
                                        };
                                        setTimeout(check, 500);
                                    });
                                }""", date_num)
                                page.wait_for_timeout(2000)

                                # 선택된 날짜 확인 - itemActive 클래스로 검증
                                selected_date_info = page.evaluate(r"""() => {
                                    var buttons = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                                    for (var i = 0; i < buttons.length; i++) {
                                        if (buttons[i].className.indexOf('Active') !== -1 || buttons[i].className.indexOf('active') !== -1) {
                                            var spans = buttons[i].querySelectorAll('span');
                                            if (spans.length >= 2) {
                                                return {day: spans[0].innerText.trim(), date: spans[1].innerText.trim().replace(/^0/, '')};
                                            }
                                        }
                                    }
                                    return null;
                                }""")

                                # 클릭한 날짜와 선택된 날짜가 다르면 스킵
                                if selected_date_info:
                                    sel_date = selected_date_info.get('date', '')
                                    if sel_date != date_num:
                                        sel_day = selected_date_info.get('day', '')
                                        print(f"    날짜 불일치 스킵: 클릭={day}{date_num}, 선택={sel_day}{sel_date}")
                                        continue

                                # 스케줄이 있는지 확인
                                has_schedule = page.evaluate("""() => {
                                    var bodyText = document.body.innerText || '';
                                    // 스케줄이 없는 경우 메시지 확인
                                    if (bodyText.indexOf('상영일정이 없습니다') !== -1 ||
                                        bodyText.indexOf('상영정보가 없습니다') !== -1 ||
                                        bodyText.indexOf('예매 가능한 상영시간이 없습니다') !== -1) {
                                        return false;
                                    }
                                    // 시간 패턴이 있는지 확인 (HH:MM)
                                    var timePattern = /\\d{1,2}:\\d{2}/;
                                    var scheduleArea = document.body.innerText.substring(
                                        document.body.innerText.indexOf('전체') || 0
                                    );
                                    return timePattern.test(scheduleArea);
                                }""")

                                if not has_schedule:
                                    print(f"    스케줄 없음 스킵: {day} {date_num}")
                                    continue

                                # 페이지 스크롤하여 모든 영화 로드
                                page.evaluate("""() => {
                                    window.scrollTo(0, document.body.scrollHeight);
                                }""")
                                page.wait_for_timeout(600)
                                page.evaluate("""() => {
                                    window.scrollTo(0, 0);
                                }""")
                                page.wait_for_timeout(400)

                                # 상영 시간표에서 영화별 무대인사/GV/시네마톡 추출 (스케줄 영역만 정확하게 파싱)
                                movie_events = page.evaluate("""() => {
                                    var results = [];

                                    // 방법 1: 시간 블록에서 직접 무대인사 태그 찾기 (가장 정확)
                                    var timeBlocks = document.querySelectorAll('[class*="time"], [class*="schedule"], [class*="showtime"], li');

                                    for (var i = 0; i < timeBlocks.length; i++) {
                                        var block = timeBlocks[i];
                                        var blockText = block.innerText || '';
                                        var rect = block.getBoundingClientRect();

                                        // 화면 하단의 스케줄 영역만 확인 (y > 400)
                                        if (rect.top < 400) continue;

                                        // 시간 패턴 확인 (HH:MM)
                                        var timeMatch = blockText.match(/(\d{1,2}:\d{2})/);
                                        if (!timeMatch) continue;

                                        // 시간이 3개 이상이면 컨테이너 → 스킵 (개별 카드는 시작+종료 2개)
                                        var allTimes = blockText.match(/\d{1,2}:\d{2}/g);
                                        if (allTimes && allTimes.length > 2) continue;

                                        var timeStr = timeMatch[1];

                                        // 같은 블록 내에서 이벤트 태그 확인
                                        var hasEvent = false;
                                        var eventType = '';
                                        var isPreparing = false;

                                        if (blockText.indexOf('무대인사') !== -1) {
                                            hasEvent = true;
                                            eventType = '무대인사';
                                        }
                                        if (blockText.indexOf('시네마톡') !== -1) {
                                            hasEvent = true;
                                            eventType = '시네마톡';
                                        }
                                        if (blockText.indexOf('굿즈') !== -1) {
                                            hasEvent = true;
                                            eventType = '굿즈';
                                        }
                                        if (blockText.indexOf('프리미어') !== -1) {
                                            hasEvent = true;
                                            eventType = '프리미어 상영';
                                        }
                                        if (blockText.indexOf('예매 준비중') !== -1 || blockText.indexOf('예매준비중') !== -1) {
                                            isPreparing = true;
                                        }

                                        // 매진 상태 확인
                                        var isSoldOut = false;
                                        if (blockText.indexOf('매진') !== -1 || blockText.indexOf('마감') !== -1) {
                                            isSoldOut = true;
                                        }

                                        if (!hasEvent) continue;

                                        // 영화 제목 찾기 - 상위 요소에서 검색
                                        var movieName = '';
                                        var parent = block.parentElement;
                                        for (var p = 0; p < 10 && parent; p++) {
                                            var parentText = parent.innerText || '';
                                            var lines = parentText.split('\\n');
                                            for (var l = 0; l < lines.length; l++) {
                                                var line = lines[l].trim();
                                                // 영화 제목 패턴: 한글로 시작, 2~30자, 시간/석/이벤트/상태 아님
                                                if (/^[가-힣]/.test(line) &&
                                                    line.length >= 2 && line.length <= 30 &&
                                                    !/^\d/.test(line) &&
                                                    !/석$/.test(line) &&
                                                    !/(무대인사|시네마톡|GV|굿즈|프리미어|전체|오전|오후|심야)/.test(line) &&
                                                    !/^(2D|3D|IMAX|Laser|\d+관)/.test(line) &&
                                                    !/^(매진|마감|예매|예매 준비중|잔여|좌석|상영중|상영예정|개봉|특별관|일반|자막|더빙|프리미어 상영|조조|심야|응원 상영회|응원상영)$/.test(line) &&
                                                    line.indexOf('시네마') === -1 && line.indexOf('CINE') === -1 &&
                                                    line.indexOf('[') === -1 && line.indexOf('템퍼') === -1 && line.indexOf('씨네앤') === -1 && line.indexOf('리빙룸') === -1 &&
                                                    line.indexOf('스트레스리스') === -1 && line.indexOf('프리미엄') === -1 &&
                                                    line.indexOf('골드클래스') === -1 && line.indexOf('SCREENX') === -1 &&
                                                    line.indexOf('DOLBY') === -1 && line.indexOf('PRIVATE') === -1 &&
                                                    line.indexOf('PREMIUM') === -1 && line.indexOf('리클라이너') === -1) {
                                                    movieName = line;
                                                    break;
                                                }
                                            }
                                            if (movieName) break;
                                            parent = parent.parentElement;
                                        }

                                        if (movieName && timeStr) {
                                            // 중복 체크
                                            var isDup = false;
                                            for (var r = 0; r < results.length; r++) {
                                                if (results[r].movie === movieName && results[r].time === timeStr) {
                                                    isDup = true;
                                                    break;
                                                }
                                            }
                                            if (!isDup) {
                                                results.push({
                                                    movie: movieName,
                                                    time: timeStr,
                                                    eventType: eventType,
                                                    preparing: isPreparing,
                                                    soldOut: isSoldOut
                                                });
                                            }
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
                                        is_sold_out = event.get("soldOut", False)

                                        greeting_id = f"{theater}_{current_year}_{current_month}_{date_num}_{time_str}_{movie_name[:10]}"

                                        if greeting_id not in [x["id"] for x in all_greetings]:
                                            status_str = " [예매준비중]" if is_preparing else (" [매진]" if is_sold_out else "")
                                            print(f"    - [{event_type}] {movie_name} {time_str}{status_str}")
                                            g = {
                                                "movie": movie_name,
                                                "theater": f"CGV {theater}",
                                                "date": date_str,
                                                "time": time_str,
                                                "hall": "",
                                                "event_type": event_type,
                                                "id": greeting_id,
                                                "preparing": is_preparing,
                                                "sold_out": is_sold_out
                                            }
                                            all_greetings.append(g)
                                else:
                                    print(f"  {day}요일 {date_num}일 이벤트 없음")
                            except Exception as e:
                                print(f"  {day}요일 {date_num}일 오류: {e}")

                        # Swiper 다음 버튼으로 날짜 범위 이동
                        arrow_clicked = page.evaluate(
                            r"""() => {
                            var nextBtn = document.querySelector('.swiper-button-next');
                            if (nextBtn && !nextBtn.classList.contains('swiper-button-disabled')) {
                                nextBtn.click();
                                return true;
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

    # 기존 이벤트를 ID로 매핑
    saved_by_id = {g.get("id"): g for g in saved_data.get("greetings", []) if g.get("id")}

    new_greetings = []
    preparing_greetings = []  # 새로 감지된 예매 준비중
    sales_started_greetings = []  # 예매 준비중 → 예매 시작
    reopened_greetings = []  # 매진 → 예매 가능 (취소표)

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
            if old_g:
                # 예매 준비중 → 예매 시작
                if old_g.get("preparing") and not g.get("preparing"):
                    sales_started_greetings.append(g)
                    old_g["preparing"] = False

                # 매진 → 예매 가능 (취소표 발생)
                if old_g.get("sold_out") and not g.get("sold_out"):
                    reopened_greetings.append(g)
                    old_g["sold_out"] = False

                # 예매 가능 → 매진 (상태 업데이트만, 알림 없음)
                if not old_g.get("sold_out") and g.get("sold_out"):
                    old_g["sold_out"] = True

    # 알림 전송
    if new_greetings:
        print(f"새 이벤트 {len(new_greetings)}개!")
        for g in new_greetings:
            if g.get("preparing"):
                send_discord_notification(g, "preparing")
            else:
                send_discord_notification(g, "new")
        saved_data["greetings"].extend(new_greetings)

    if sales_started_greetings:
        print(f"예매 오픈 {len(sales_started_greetings)}개!")
        for g in sales_started_greetings:
            send_discord_notification(g, "sales_started")

    if reopened_greetings:
        print(f"취소표 발생 {len(reopened_greetings)}개!")
        for g in reopened_greetings:
            send_discord_notification(g, "reopened")

    if new_greetings or sales_started_greetings or reopened_greetings:
        save_data(saved_data)
    else:
        print("새 이벤트 없음")


if __name__ == "__main__":
    main()
