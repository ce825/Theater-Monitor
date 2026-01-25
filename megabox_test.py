#!/usr/bin/env python3
"""메가박스 모니터링 테스트"""

import re
from datetime import datetime
from playwright.sync_api import sync_playwright

MEGABOX_THEATERS = [
    ("서울", "코엑스"),
    ("서울", "홍대"),
    ("서울", "목동"),
    ("서울", "구의이스트폴"),
]


def check_megabox_greetings():
    """메가박스 타겟 극장들의 주말 무대인사/GV/시네마톡 확인"""
    all_greetings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                viewport={'width': 1920, 'height': 1080}
            )
            page = context.new_page()

            # 메인 페이지에서 빠른예매로 이동
            page.goto('https://www.megabox.co.kr/', timeout=60000)
            page.wait_for_timeout(2000)
            page.click('text=빠른예매', timeout=5000)
            page.wait_for_timeout(5000)

            # 예매 iframe 찾기
            booking_frame = None
            for frame in page.frames:
                if 'SimpleBooking' in frame.url:
                    booking_frame = frame
                    break

            if not booking_frame:
                print("[메가박스] 예매 iframe을 찾지 못함")
                browser.close()
                return all_greetings

            print("[메가박스] 예매 iframe 발견")

            # 영화 목록 추출
            movies = booking_frame.evaluate("""() => {
                const text = document.body.innerText;
                const lines = text.split('\\n');
                const movies = [];
                let inMovieSection = false;
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (trimmed === '영화') {
                        inMovieSection = true;
                        continue;
                    }
                    if (trimmed === '극장' || trimmed === '시간') {
                        break;
                    }
                    if (inMovieSection && trimmed.length > 1 &&
                        !trimmed.includes('세이상관람가') &&
                        !trimmed.includes('전체관람가') &&
                        !trimmed.includes('청소년관람불가') &&
                        !trimmed.includes('보고싶어') &&
                        !trimmed.includes('전체') &&
                        !trimmed.includes('큐레이션')) {
                        movies.push(trimmed);
                    }
                }
                return movies;
            }""")

            print(f"[메가박스] 영화 {len(movies)}개 발견: {movies[:5]}...")

            # 타겟 극장 이름
            theater_names = [t[1] for t in MEGABOX_THEATERS]

            # 3개씩 영화 배치 처리
            batch_size = 3
            for batch_start in range(0, min(len(movies), 9), batch_size):  # 테스트용 9개만
                batch_movies = movies[batch_start:batch_start + batch_size]
                print(f"\n{'='*50}")
                print(f"[메가박스] 배치 {batch_start//batch_size + 1}: {batch_movies}")
                print('='*50)

                try:
                    # 새로고침 (첫 배치 제외)
                    if batch_start > 0:
                        page.goto('https://www.megabox.co.kr/', timeout=60000)
                        page.wait_for_timeout(2000)
                        page.click('text=빠른예매', timeout=5000)
                        page.wait_for_timeout(5000)

                        # iframe 다시 찾기
                        booking_frame = None
                        for frame in page.frames:
                            if 'SimpleBooking' in frame.url:
                                booking_frame = frame
                                break
                        if not booking_frame:
                            continue

                    # 1. 영화 3개 클릭
                    for movie in batch_movies:
                        clicked = booking_frame.evaluate("""(movieTitle) => {
                            const elements = document.querySelectorAll('*');
                            for (const el of elements) {
                                if (el.innerText && el.innerText.trim() === movieTitle &&
                                    el.tagName !== 'BODY' && el.tagName !== 'HTML') {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""", movie)
                        if clicked:
                            print(f"  영화 선택: {movie}")
                        booking_frame.wait_for_timeout(300)

                    # 2. 서울 지역 클릭
                    try:
                        booking_frame.click("text=/서울\\(\\d+\\)/", timeout=3000)
                        booking_frame.wait_for_timeout(1000)
                        print("  서울 지역 클릭")
                    except:
                        print("  서울 지역 클릭 실패")

                    # 3. 타겟 극장 4개 클릭
                    for theater in theater_names:
                        clicked = booking_frame.evaluate("""(theaterName) => {
                            const elements = document.querySelectorAll('*');
                            for (const el of elements) {
                                if (el.innerText && el.innerText.trim() === theaterName) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""", theater)
                        if clicked:
                            print(f"  극장 선택: {theater}")
                        booking_frame.wait_for_timeout(300)

                    booking_frame.wait_for_timeout(1000)

                    # 4. 주말 날짜 클릭 및 확인
                    frame_text = booking_frame.inner_text("body")

                    # 토/일 날짜 찾기 - 패턴: "31\n일\n토" (날짜, 요일표시, 실제요일)
                    weekend_dates = []
                    lines = frame_text.split('\n')
                    for i, line in enumerate(lines):
                        line_stripped = line.strip()
                        # "토" 또는 "일"이 요일로 나타나는 경우
                        if line_stripped == '토' and i >= 2:
                            # 2줄 위에서 날짜 찾기 (31\n일\n토 패턴)
                            date_line = lines[i-2].strip()
                            if date_line.isdigit():
                                weekend_dates.append({"day": "토", "date": date_line})
                        elif line_stripped == '일' and i >= 2:
                            date_line = lines[i-2].strip()
                            # "일"이 날짜 표시 "일"이 아닌 실제 일요일인지 확인
                            if date_line.isdigit() and lines[i-1].strip() == '일':
                                weekend_dates.append({"day": "일", "date": date_line})

                    # 중복 제거
                    seen = set()
                    unique_dates = []
                    for d in weekend_dates:
                        key = f"{d['day']}_{d['date']}"
                        if key not in seen:
                            seen.add(key)
                            unique_dates.append(d)

                    print(f"  주말 날짜: {[(d['date'], d['day']) for d in unique_dates]}")

                    for date_info in unique_dates:
                        day = date_info["day"]
                        date_num = date_info["date"]

                        # 날짜 클릭
                        date_clicked = booking_frame.evaluate(
                            """(args) => {
                            var dateNum = args.dateNum;
                            var dayType = args.dayType;
                            var elements = document.querySelectorAll('a, button, li, div, span');
                            for (var i = 0; i < elements.length; i++) {
                                var el = elements[i];
                                var text = (el.innerText || '').trim();
                                if (text.includes(dateNum) && text.includes(dayType)) {
                                    if (text.length < 20) {
                                        el.click();
                                        return {success: true, text: text};
                                    }
                                }
                            }
                            return {success: false};
                        }""", {"dateNum": date_num, "dayType": day})

                        if not date_clicked.get('success'):
                            print(f"    날짜 클릭 실패: {date_num} {day}")
                            continue

                        booking_frame.wait_for_timeout(2000)
                        print(f"    날짜 클릭 성공: {date_num} ({day})")

                        # 5. 무대인사 확인
                        body = booking_frame.inner_text("body")
                        found_events = []
                        if "무대인사" in body:
                            found_events.append("무대인사")
                        if "시네마톡" in body:
                            found_events.append("시네마톡")
                        if re.search(r'(?<!E)GV(?!I)', body):
                            found_events.append("GV")

                        if found_events:
                            print(f"    ★ 이벤트 발견: {', '.join(found_events)}")

                            # 날짜 계산
                            today = datetime.now()
                            target_day = int(date_num)
                            if target_day >= today.day:
                                current_month = today.month
                                current_year = today.year
                            else:
                                current_month = today.month + 1 if today.month < 12 else 1
                                current_year = today.year if today.month < 12 else today.year + 1

                            date_str = f"{current_month}월 {date_num}일 ({day})"

                            # 시간 추출
                            lines = body.split('\n')
                            for i, line in enumerate(lines):
                                if any(ev in line for ev in found_events):
                                    for j in range(max(0, i-10), min(len(lines), i+10)):
                                        tm = re.search(r'(\d{1,2}:\d{2})', lines[j])
                                        if tm:
                                            time_str = tm.group(1)
                                            event_type_str = "/".join(found_events)
                                            movie_name = batch_movies[0] if batch_movies else "미정"

                                            # 극장 찾기
                                            theater_found = ""
                                            for t in theater_names:
                                                if t in body:
                                                    theater_found = t
                                                    break

                                            greeting_id = f"megabox_{theater_found}_{current_year}_{current_month}_{date_num}_{time_str}_{movie_name[:5]}"

                                            if greeting_id not in [x["id"] for x in all_greetings]:
                                                print(f"      [{event_type_str}] {movie_name} @ 메가박스 {theater_found} {time_str}")
                                                g = {
                                                    "movie": movie_name,
                                                    "theater": f"메가박스 {theater_found}" if theater_found else "메가박스",
                                                    "date": date_str,
                                                    "time": time_str,
                                                    "hall": "",
                                                    "event_type": event_type_str,
                                                    "id": greeting_id
                                                }
                                                all_greetings.append(g)
                                            break
                        else:
                            print(f"    이벤트 없음")

                except Exception as e:
                    print(f"  배치 오류: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            browser.close()
            print("\n" + "="*50)
            print(f"메가박스 확인 완료! {len(all_greetings)}개 발견")

    except Exception as e:
        print(f"메가박스 오류: {e}")
        import traceback
        traceback.print_exc()

    return all_greetings


if __name__ == "__main__":
    result = check_megabox_greetings()
    print("\n=== 최종 결과 ===")
    print(f"총 {len(result)}개 발견")
    for r in result:
        print(f"  {r['theater']} | {r['date']} {r['time']} | {r['movie']} | {r['event_type']}")
