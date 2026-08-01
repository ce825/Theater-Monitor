"""
CGV 내부 예매 API 클라이언트 (Playwright 대체용)

cgv.co.kr 예매 페이지가 실제로 호출하는 JSON API를 직접 사용한다.
Cloudflare가 TLS 지문으로 봇을 차단하므로 curl_cffi의 브라우저 임퍼소네이션이 필요하다
(requests/urllib은 403).

레이트리밋 실측값 (2026-08 기준):
  - 직렬 무페이싱: 58회째부터 429
  - 1 req/s: 60회 연속 정상
  - 429 이후 회복: 약 20초
따라서 기본 페이싱을 0.7초로 두고, 429는 20초 백오프 후 재시도한다.

주요 엔드포인트:
  searchAllRegionAndSite       - 지역/극장 목록 (siteNo 매핑)
  searchSiteScnscYmdListBySite - 극장별 상영 가능 날짜
  searchMovScnInfo             - 극장+날짜의 전체 상영 회차 (siteNo 필수, 다중 지정 불가)
"""

import re
import threading
import time
from datetime import datetime

from curl_cffi import requests as cffi_requests

API_BASE = "https://cgv.co.kr/api/v1"
CO_CD = "A420"
DEFAULT_HEADERS = {
    "Referer": "https://cgv.co.kr/cnm/movieBook/cinema",
    "Accept": "application/json",
}

# videoAddexpCdNm(영상 부가설명 코드명) 중 실제 이벤트로 취급할 값.
# 제외: '기타'(SCREENX 4면 표기 등 노이즈),
#       '씨네드쉐프행사'([모닝 반값 특가] 같은 할인 상영에도 붙어서 오탐이 난다)
EVENT_TAGS = {"무대인사", "시네마톡", "GV", "굿즈패키지", "관객과의 대화"}

# 태그 필드가 비어 있는 경우를 위한 제목 괄호 안 키워드 폴백.
TITLE_EVENT_PATTERNS = [
    ("무대인사", "무대인사"),
    ("시네마톡", "시네마톡"),
    ("씨네토크", "시네마톡"),
    ("관객과의 대화", "GV"),
    ("프리미어 상영회", "프리미어 상영"),
    ("프리미어상영", "프리미어 상영"),
    ("굿즈", "굿즈"),
    ("라이브톡", "라이브톡"),
]

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


class RateLimited(Exception):
    pass


class CGVClient:
    """페이싱 + 재시도가 내장된 CGV API 클라이언트."""

    def __init__(self, min_interval=0.7, impersonate="chrome", timeout=20):
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = cffi_requests.Session(impersonate=impersonate, headers=DEFAULT_HEADERS)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.request_count = 0
        self.retry_count = 0
        self.debug = False

    def _pace(self):
        """전역 페이싱 - 레이트리밋에 걸리지 않도록 요청 간격을 강제한다."""
        with self._lock:
            gap = time.time() - self._last_call
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last_call = time.time()

    def get(self, path, params=None, tries=4):
        url = f"{API_BASE}/{path}"
        params = dict(params or {})
        params.setdefault("coCd", CO_CD)

        last_err = None
        for attempt in range(tries):
            self._pace()
            try:
                self.request_count += 1
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("statusCode") not in (0, "0", None):
                        raise RuntimeError(f"API 오류: {body.get('statusMessage')}")
                    return body.get("data")
                if resp.status_code == 429:
                    self.retry_count += 1
                    if self.debug:
                        print(f"    [429] {path} {params} - {20 * (attempt + 1)}초 대기")
                    last_err = RateLimited(f"429 {path}")
                    time.sleep(20 * (attempt + 1))
                    continue
                last_err = RuntimeError(f"HTTP {resp.status_code} {path}")
            except Exception as e:  # 네트워크 오류/JSON 파싱 실패(차단 페이지) 포함
                last_err = e
            self.retry_count += 1
            if self.debug:
                print(f"    [재시도 {attempt}] {path} {params} - {last_err}")
            time.sleep(2 * (attempt + 1))

        raise last_err if last_err else RuntimeError(f"요청 실패: {path}")

    # ---- 엔드포인트 래퍼 ------------------------------------------------

    def site_map(self):
        """{극장명: siteNo} 매핑. 예: {'용산아이파크몰': '0013'}"""
        data = self.get("content/site/searchAllRegionAndSite")
        return {s["siteNm"]: s["siteNo"] for s in (data.get("siteInfo") or [])}

    def screening_dates(self, site_no):
        """극장의 상영 가능 날짜. [{'ymd': '20260808', 'holiday': False}, ...]"""
        data = self.get("booking/searchSiteScnscYmdListBySite", {"siteNo": site_no})
        return [{"ymd": d["scnYmd"], "holiday": d.get("hldyYn") == "Y"} for d in (data or [])]

    def schedule(self, site_no, ymd):
        """극장+날짜의 전체 상영 회차 원본 레코드."""
        data = self.get(
            "booking/searchMovScnInfo",
            {"siteNo": site_no, "scnYmd": ymd, "rtctlScopCd": "08"},
        )
        return data or []


# ---- 파싱 ---------------------------------------------------------------


def _format_time(hhmm):
    """'1105' -> '11:05'. CGV는 심야를 25:30처럼 24시 이상으로 표기한다."""
    hhmm = (hhmm or "").strip()
    if len(hhmm) != 4 or not hhmm.isdigit():
        return hhmm
    return f"{hhmm[:2]}:{hhmm[2:]}"


def _format_date(ymd):
    """'20260808' -> '8월 8일 (토)'"""
    try:
        d = datetime.strptime(ymd, "%Y%m%d")
    except ValueError:
        return ymd
    return f"{d.month}월 {d.day}일 ({WEEKDAY_KR[d.weekday()]})"


def detect_event_type(record):
    """
    상영 회차가 무대인사/GV류 이벤트인지 판별하고 이벤트명을 반환. 아니면 None.

    1순위: videoAddexpCdNm (CGV가 회차에 직접 붙이는 부가설명 코드)
    2순위: 노출 상품명(expoProdNm) 괄호 안 키워드
    """
    tag = (record.get("videoAddexpCdNm") or "").strip()
    if tag in EVENT_TAGS:
        return tag

    title = record.get("expoProdNm") or record.get("prodNm") or ""
    # 괄호 안 텍스트만 검사한다. 영화 제목 자체에 '무대인사'가 들어갈 일은 없고,
    # 괄호 밖까지 보면 오탐이 늘어난다.
    suffix = " ".join(re.findall(r"\(([^)]*)\)", title))
    for keyword, event_name in TITLE_EVENT_PATTERNS:
        if keyword in suffix:
            return event_name

    # 태그가 '기타'인데 제목에 이벤트 키워드가 없으면 특별관 표기 등 노이즈로 본다.
    return None


def parse_record(record, theater_name, event_type):
    """API 회차 레코드를 모니터 내부 포맷으로 변환한다."""
    ymd = record.get("scnYmd", "")
    time_str = _format_time(record.get("scnsrtTm"))
    movie = (record.get("movNm") or record.get("prodNm") or "미정").strip()

    free_seats = int(record.get("frSeatCnt") or 0)
    total_seats = int(record.get("cpSeatCnt") or record.get("stcnt") or 0)
    controlled = record.get("cntlYn") == "Y"

    # 매진: 잔여석 0
    # 예매 준비중: 좌석은 남아 있는데 통제(cntlYn=Y) 상태 → 아직 판매 개시 전
    sold_out = free_seats == 0
    preparing = controlled and free_seats > 0

    return {
        "movie": movie,
        "theater": f"CGV {theater_name}",
        "date": _format_date(ymd),
        "time": time_str,
        "hall": (record.get("expoScnsNm") or record.get("scnsNm") or "").strip(),
        "event_type": event_type,
        "id": f"{theater_name}_{ymd}_{time_str}_{movie[:10]}",
        "preparing": preparing,
        "sold_out": sold_out,
        # 부가 정보 (알림 본문/디버깅용)
        "ymd": ymd,
        "seats_left": free_seats,
        "seats_total": total_seats,
        "screen_type": (record.get("movkndDsplNm") or "").strip(),
        "subtitle": (record.get("sbtdivNm") or "").strip(),
        "prod_no": record.get("prodNo"),
        "full_title": (record.get("expoProdNm") or "").strip(),
    }


def extract_events(records, theater_name):
    """상영 회차 목록에서 이벤트 상영만 골라 정규화한다."""
    events = []
    seen_ids = set()
    for r in records:
        event_type = detect_event_type(r)
        if not event_type:
            continue
        e = parse_record(r, theater_name, event_type)
        # 같은 극장/날짜/시각/영화가 서로 다른 관에서 열리면 id가 겹친다 → 관 이름으로 구분
        if e["id"] in seen_ids:
            e["id"] = f"{e['id']}_{e['hall']}"
        seen_ids.add(e["id"])
        events.append(e)
    return events


# tcscnsGradNm(특별관 등급명) → 알림에 쓸 라벨
SPECIAL_HALL_LABELS = {"아이맥스": "IMAX", "4DX": "4DX", "SCREENX": "SCREENX"}


def detect_special_hall(record, wanted):
    """
    IMAX/4DX 등 특별관 상영인지 판별해 라벨을 반환. 아니면 None.

    tcscnsGradNm이 '아이맥스'/'4DX'/'SCREENX'로 이미 분류돼 있어 문자열 추측이 필요 없다.
    """
    label = SPECIAL_HALL_LABELS.get((record.get("tcscnsGradNm") or "").strip())
    return label if label and label in wanted else None


def extract_special_screenings(records, theater_name, wanted=("IMAX", "4DX")):
    """특별관(IMAX/4DX 등) 상영 회차만 골라 정규화한다."""
    wanted = set(wanted)
    showings = []
    seen_ids = set()
    for r in records:
        label = detect_special_hall(r, wanted)
        if not label:
            continue
        s = parse_record(r, theater_name, label)
        # 기존 IMAX 모니터와 동일하게 상영관 정보까지 id에 포함한다
        s["hall_label"] = label
        s["id"] = f"{theater_name}_{s['ymd']}_{s['time']}_{s['movie'][:10]}_{s['hall']}"
        if s["id"] in seen_ids:
            continue
        seen_ids.add(s["id"])
        showings.append(s)
    return showings


# ---- 스캔 ---------------------------------------------------------------


def is_target_date(ymd, holiday_flag, weekends_only=True, holiday_checker=None):
    """주말 또는 공휴일인지 판정. 무대인사는 사실상 주말/공휴일에만 열린다."""
    if not weekends_only:
        return True
    try:
        d = datetime.strptime(ymd, "%Y%m%d")
    except ValueError:
        return False
    if d.weekday() >= 5:
        return True
    if holiday_flag:
        return True
    if holiday_checker and d.date() in holiday_checker:
        return True
    return False


def _theater_name(t):
    return t[1] if isinstance(t, (tuple, list)) else t


def build_jobs(client, specs, holiday_checker=None, date_cache=None):
    """
    조회할 (극장, 날짜) 슬롯 목록을 만든다.

    specs: [{"key": 트랙이름, "theaters": [...], "weekends_only": bool}, ...]
           여러 모니터가 같은 극장/날짜를 요구하면 슬롯을 합쳐 요청을 한 번만 보낸다.
           각 슬롯에 "그 슬롯을 요구한 트랙"을 기록해 두므로, 호출부가 날짜 조건을
           다시 판정할 필요가 없다 (CGV가 알려주는 공휴일 플래그를 잃지 않도록).
    반환: [(극장명, siteNo, ymd, {트랙이름, ...}), ...]
    """
    sites = client.site_map()
    slots = {}  # (극장명, ymd) -> [siteNo, {키}]

    for i, spec in enumerate(specs):
        key = spec.get("key", i)
        weekends_only = spec.get("weekends_only", True)
        for t in spec["theaters"]:
            name = _theater_name(t)
            site_no = sites.get(name)
            if not site_no:
                print(f"  [경고] 극장 코드를 찾을 수 없음: {name}")
                continue

            if date_cache is not None and site_no in date_cache:
                dates = date_cache[site_no]
            else:
                dates = client.screening_dates(site_no)
                if date_cache is not None:
                    date_cache[site_no] = dates

            for d in dates:
                if is_target_date(d["ymd"], d["holiday"], weekends_only, holiday_checker):
                    slot = slots.setdefault((name, d["ymd"]), [site_no, set()])
                    slot[1].add(key)

    return [(name, site_no, ymd, keys)
            for (name, ymd), (site_no, keys) in sorted(slots.items())]


def fetch_jobs(client, jobs):
    """슬롯을 순회하며 원본 회차 레코드를 내보낸다. -> (극장명, ymd, 요구 트랙, 레코드)"""
    for name, site_no, ymd, keys in jobs:
        yield name, ymd, keys, client.schedule(site_no, ymd)


def scan(client, theaters, weekends_only=True, holiday_checker=None,
         date_cache=None, verbose=False):
    """
    대상 극장들의 이벤트 상영을 전부 수집한다. (무대인사 단독 스캔용 - 검증/단발 실행)

    theaters: [(지역, 극장명), ...] 또는 [극장명, ...]
    date_cache: {siteNo: [dates]} 형태의 캐시 dict. 넘기면 날짜 목록 재조회를 생략한다
                (상영 날짜 목록은 자주 바뀌지 않으므로 요청 수 절약).
    반환: (events, stats)
    """
    stats = {"date_slots": 0, "showtimes": 0, "requests_start": client.request_count}
    jobs = build_jobs(
        client,
        [{"key": "stage", "theaters": theaters, "weekends_only": weekends_only}],
        holiday_checker=holiday_checker,
        date_cache=date_cache,
    )
    stats["theaters"] = len({j[0] for j in jobs})

    events = []
    for name, ymd, _keys, records in fetch_jobs(client, jobs):
        stats["date_slots"] += 1
        stats["showtimes"] += len(records)
        found = extract_events(records, name)
        events.extend(found)
        if verbose and found:
            for e in found:
                print(f"    ★ [{e['event_type']}] {e['movie']} {e['date']} {e['time']} "
                      f"({e['hall']}, 잔여 {e['seats_left']}/{e['seats_total']})")

    stats["requests"] = client.request_count - stats["requests_start"]
    stats["events"] = len(events)
    return events, stats
