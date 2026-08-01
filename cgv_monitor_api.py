#!/usr/bin/env python3
"""
CGV 통합 모니터 (API 방식) — 무대인사/GV/시네마톡 + IMAX/4DX

기존 Playwright 스크래핑(cgv_monitor_actions.py 약 3분 47초, cgv_imax_monitor.py 별도 실행)을
CGV 내부 JSON API로 대체한 버전. 1회 스캔이 약 40초로 줄어들어 GitHub Actions 실행 1회 안에서
루프를 돌며 1분 간격으로 감시할 수 있다.

두 모니터를 한 프로세스에서 돌리는 이유:
  - CGV API에 레이트리밋이 있어(약 5 req/s, 429 후 20초 정지) 워크플로를 따로 돌리면 서로 걸린다
  - 용산아이파크몰 주말은 양쪽이 같은 데이터를 쓰므로 요청을 공유하면 요청 수가 줄어든다
상태 파일과 웹훅은 기존과 동일하게 분리되어 있어 알림 동작은 그대로다.

사용법:
    python cgv_monitor_api.py --once                      # 1회 스캔
    python cgv_monitor_api.py --once --dry-run            # 알림 없이 결과만 출력
    python cgv_monitor_api.py --loop 3000 --interval 60   # 50분간 60초 간격 감시
    python cgv_monitor_api.py --once --track stage        # 무대인사만

환경변수:
    DISCORD_WEBHOOK_URL       무대인사 알림 웹훅
    IMAX_DISCORD_WEBHOOK_URL  IMAX/4DX 알림 웹훅 (미설정 시 기존 하드코딩 값)
    HEALTHCHECK_URL           매 사이클 핑을 보낼 healthchecks.io URL (선택)
    LOOP_SECONDS / LOOP_INTERVAL   --loop / --interval 기본값
"""

import argparse
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

import cgv_api

try:
    import holidays
    KR_HOLIDAYS = holidays.KR()
except ImportError:  # 공휴일 정보가 없어도 주말 감시는 동작한다
    KR_HOLIDAYS = None

CGV_URL = "https://cgv.co.kr/cnm/movieBook"

# 파서가 바뀌면 id 체계가 달라져 기존 상태와 대조할 수 없다.
# 저장된 값과 다르면 알림 없이 기준선만 다시 잡는다.
PARSER_VERSION = "api-v1"

# 상영 가능 날짜 목록은 자주 바뀌지 않으므로 캐시해서 요청 수를 줄인다.
DATE_CACHE_TTL = 600  # 초

STAGE_THEATERS = [
    ("서울", "용산아이파크몰"),
    ("서울", "영등포타임스퀘어"),
    ("서울", "왕십리"),
    ("서울", "건대입구"),
    ("서울", "강변"),
    ("서울", "여의도"),
    ("서울", "압구정"),
    ("서울", "홍대"),
]

IMAX_THEATERS = [("서울", "용산아이파크몰")]

IMAX_WEBHOOK_DEFAULT = ("https://discord.com/api/webhooks/1464630439116410963/"
                        "NWuBIWCBPmlajS4sXmZ9P-P53OKmQt48rFt8im6Yo3NDkc4-ohC0SY6ZPt5R8C3Owp3y")

STATUS_LABEL = {"preparing": "예매준비중", "sales_started": "예매오픈",
                "reopened": "취소표", "new": "신규"}

# 인기 회차는 잔여석이 0과 1 사이를 계속 오간다(취소 → 즉시 예매 → 또 취소).
# 1분 간격으로 보면 같은 회차의 취소표 알림이 몇 분 간격으로 반복되므로,
# 같은 회차·같은 종류의 알림은 이 시간 안에 한 번만 보낸다.
NOTIFY_COOLDOWN = {"reopened": 1800, "sales_started": 600, "preparing": 1800}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def post_discord(webhook, payload):
    if not webhook:
        return False
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log(f"  Discord HTTP {resp.status_code}")
            return False
        return True
    except requests.RequestException as e:
        log(f"  Discord 오류: {e}")
        return False


def ping_healthcheck():
    url = os.environ.get("HEALTHCHECK_URL", "")
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except requests.RequestException:
        pass


# ---- 알림 본문 ----------------------------------------------------------


def stage_embed(item, kind):
    event_type = item.get("event_type", "무대인사")
    title, color, footer = {
        "preparing": (f"⏳ {event_type} 예매 준비중!", 0xFFA500, f"CGV {event_type} - 예매 준비중"),
        "sales_started": (f"🎟️ {event_type} 예매 오픈!", 0x00FF00, f"CGV {event_type} - 예매 시작"),
        "reopened": (f"🔄 {event_type} 취소표 발생!", 0x9932CC, f"CGV {event_type} - 매진 → 예매 가능"),
    }.get(kind, (f"🆕 새로운 {event_type} 일정 등록!", 0xED1C24, f"CGV {event_type} 알림"))

    fields = [
        {"name": "🎬 영화", "value": item.get("movie", "미정"), "inline": False},
        {"name": "🎫 이벤트", "value": event_type, "inline": True},
        {"name": "📍 극장", "value": item.get("theater", "미정"), "inline": True},
        {"name": "📅 날짜", "value": item.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": item.get("time", "미정"), "inline": True},
    ]
    if item.get("hall"):
        fields.append({"name": "🎥 상영관", "value": item["hall"], "inline": True})
    if item.get("seats_total"):
        fields.append({"name": "💺 잔여석",
                       "value": f"{item.get('seats_left', 0)} / {item['seats_total']}", "inline": True})
    return title, color, footer, fields


def imax_embed(item, kind):
    label = item.get("hall_label") or item.get("event_type", "IMAX")
    is_imax = label == "IMAX"
    title, color, footer = {
        "preparing": (f"⏳ {label} 예매 준비중!", 0xFFA500, f"CGV {label} 예매 알림"),
        "sales_started": (f"🎟️ {label} 예매 오픈!", 0x00FF00, f"CGV {label} 예매 알림"),
        "reopened": (f"🔄 {label} 취소표 발생!", 0x9932CC, f"CGV {label} 예매 알림"),
    }.get(kind, (f"🆕 {label} 상영 일정 등록!", 0x0066FF if is_imax else 0xFF4500,
                 f"CGV {label} 예매 알림"))

    fields = [
        {"name": "🎬 영화", "value": item.get("movie", "미정"), "inline": False},
        {"name": "🎥 상영관", "value": item.get("hall", label), "inline": True},
        {"name": "📍 극장", "value": item.get("theater", "미정"), "inline": True},
        {"name": "📅 날짜", "value": item.get("date", "미정"), "inline": True},
        {"name": "⏰ 시간", "value": item.get("time", "미정"), "inline": True},
    ]
    if item.get("seats_total"):
        fields.append({"name": "💺 잔여석",
                       "value": f"{item.get('seats_left', 0)} / {item['seats_total']}", "inline": True})
    return title, color, footer, fields


# ---- 모니터 트랙 ---------------------------------------------------------


class Track:
    """하나의 감시 대상(무대인사 / IMAX·4DX)에 대한 상태 + 알림 처리."""

    def __init__(self, name, theaters, weekends_only, extractor, data_file,
                 state_key, webhook, embed_builder, start_message, dry_run=False):
        self.name = name
        self.theaters = theaters
        self.weekends_only = weekends_only
        self.extractor = extractor
        self.data_file = data_file
        self.state_key = state_key
        self.webhook = webhook
        self.embed_builder = embed_builder
        self.start_message = start_message
        self.dry_run = dry_run
        self.data = self._load()

    # 상태 저장 -----------------------------------------------------------

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault(self.state_key, [])
                return data
            except (json.JSONDecodeError, OSError) as e:
                log(f"[{self.name}] 상태 파일 읽기 실패({e}) - 새로 시작")
        return {self.state_key: [], "parser": None}

    def save(self):
        self.data["parser"] = PARSER_VERSION
        # 쿨다운 기록이 무한정 쌓이지 않도록 가장 긴 쿨다운의 2배가 지난 항목은 버린다
        sent = self.data.get("notified_at")
        if sent:
            cutoff = time.time() - max(NOTIFY_COOLDOWN.values()) * 2
            self.data["notified_at"] = {k: v for k, v in sent.items() if v > cutoff}
        if self.dry_run:  # 검증 실행이 상태 파일을 덮어쓰지 않도록
            log(f"  ({self.name} 저장 생략) {len(self.items)}건")
            return
        self.data["updated_at"] = datetime.now().isoformat()
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def items(self):
        return self.data[self.state_key]

    # 알림 ---------------------------------------------------------------

    def _cooldown_active(self, item, kind):
        """같은 회차·같은 종류의 알림을 쿨다운 안에서 반복하지 않도록 막는다."""
        window = NOTIFY_COOLDOWN.get(kind)
        if not window:
            return False
        sent_at = self.data.setdefault("notified_at", {}).get(f"{item['id']}|{kind}")
        if sent_at and time.time() - sent_at < window:
            return True
        return False

    def _mark_notified(self, item, kind):
        if kind in NOTIFY_COOLDOWN:
            self.data.setdefault("notified_at", {})[f"{item['id']}|{kind}"] = time.time()

    def notify(self, item, kind):
        desc = (f"[{STATUS_LABEL.get(kind, kind)}] {item['movie']} - "
                f"{item['theater']} {item['date']} {item['time']}")
        if self.dry_run or not self.webhook:
            log(f"  ({self.name} 알림 생략) {desc}")
            return
        title, color, footer, fields = self.embed_builder(item, kind)
        ok = post_discord(self.webhook, {"embeds": [{
            "title": title, "url": CGV_URL, "color": color, "fields": fields,
            "footer": {"text": footer},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]})
        if ok:
            log(f"  [{self.name}] 알림 전송 {desc}")
        time.sleep(0.5)  # Discord 레이트리밋 여유

    def notify_plain(self, content):
        if self.dry_run or not self.webhook:
            log(f"  ({self.name} 알림 생략) {content}")
            return
        post_discord(self.webhook, {"content": content})

    # 변화 감지 ------------------------------------------------------------

    def apply(self, found):
        """이번 스캔 결과를 반영하고 알림을 보낸다. -> 보낸 알림 수"""
        # 첫 실행이거나 파서 버전이 다르면 알림 없이 기준선만 잡는다
        if self.data.get("parser") != PARSER_VERSION or not self.items:
            reason = "첫 실행" if not self.items else "파서 버전 변경"
            log(f"[{self.name}] {reason} - 알림 없이 기준선 저장 ({len(found)}건)")
            self.data[self.state_key] = found
            self.save()
            self.notify_plain(f"{self.start_message}\n{len(found)}건 추적 시작")
            return 0

        by_id = {x["id"]: x for x in self.items if x.get("id")}
        notified = 0
        changed = False

        for item in found:
            old = by_id.get(item["id"])

            if old is None:
                self.items.append(item)
                by_id[item["id"]] = item
                changed = True
                kind = "preparing" if item["preparing"] else "new"
                self.notify(item, kind)
                self._mark_notified(item, kind)
                notified += 1
                continue

            if old.get("preparing") and not item["preparing"] and not item["sold_out"]:
                kind = "sales_started"
            elif old.get("sold_out") and not item["sold_out"]:
                kind = "reopened"
            else:
                kind = None

            if kind:
                # 잔여석이 0↔1을 오가며 같은 알림이 반복되는 것을 막는다
                if self._cooldown_active(item, kind):
                    log(f"  [{self.name}] 쿨다운으로 생략 [{STATUS_LABEL[kind]}] "
                        f"{item['movie']} {item['date']} {item['time']}")
                else:
                    self.notify(item, kind)
                    self._mark_notified(item, kind)
                    notified += 1
                changed = True

            # 좌석/상태는 알림 여부와 무관하게 최신값을 유지한다
            if (old.get("preparing") != item["preparing"]
                    or old.get("sold_out") != item["sold_out"]
                    or old.get("seats_left") != item["seats_left"]):
                old.update({
                    "preparing": item["preparing"],
                    "sold_out": item["sold_out"],
                    "seats_left": item["seats_left"],
                    "seats_total": item["seats_total"],
                })
                changed = True

        if changed or notified:
            self.save()
        return notified


def build_tracks(dry_run=False, selected=("stage", "imax")):
    tracks = {}
    if "stage" in selected:
        tracks["stage"] = Track(
            name="무대인사",
            theaters=STAGE_THEATERS,
            weekends_only=True,
            extractor=lambda records, theater: cgv_api.extract_events(records, theater),
            data_file="stage_greetings.json",
            state_key="greetings",
            webhook=os.environ.get("DISCORD_WEBHOOK_URL", ""),
            embed_builder=stage_embed,
            start_message="✅ CGV 무대인사/GV/시네마톡 모니터링 시작!",
            dry_run=dry_run,
        )
    if "imax" in selected:
        tracks["imax"] = Track(
            name="IMAX/4DX",
            theaters=IMAX_THEATERS,
            weekends_only=False,  # IMAX/4DX는 평일 포함 전체 날짜
            extractor=lambda records, theater: cgv_api.extract_special_screenings(
                records, theater, wanted=("IMAX", "4DX")),
            data_file="imax_showings.json",
            state_key="showings",
            # GitHub Actions는 미설정 시크릿을 빈 문자열로 넘긴다. `or`로 받아야
            # 하드코딩 기본값이 살아난다 (커밋 612e602에서 같은 문제를 겪었음).
            webhook=os.environ.get("IMAX_DISCORD_WEBHOOK_URL") or IMAX_WEBHOOK_DEFAULT,
            embed_builder=imax_embed,
            start_message="✅ CGV IMAX/4DX 모니터링 시작! (용산아이파크몰)",
            dry_run=dry_run,
        )
    return tracks


# ---- 스캔 루프 -----------------------------------------------------------


class Monitor:
    def __init__(self, tracks, interval=60):
        self.client = cgv_api.CGVClient()
        self.tracks = tracks
        self.interval = interval
        self.date_cache = {}
        self.date_cache_at = 0.0
        self.cycle = 0

    def run_cycle(self):
        self.cycle += 1
        t0 = time.time()
        req0 = self.client.request_count

        if time.time() - self.date_cache_at > DATE_CACHE_TTL:
            self.date_cache = {}
            self.date_cache_at = time.time()

        # 두 트랙이 요구하는 (극장, 날짜) 슬롯을 합쳐서 중복 요청을 없앤다.
        # 각 슬롯이 어느 트랙 것인지 build_jobs가 기록해 주므로 여기서 날짜 조건을 다시 보지 않는다.
        jobs = cgv_api.build_jobs(
            self.client,
            [{"key": name, "theaters": t.theaters, "weekends_only": t.weekends_only}
             for name, t in self.tracks.items()],
            holiday_checker=KR_HOLIDAYS,
            date_cache=self.date_cache,
        )

        found = {name: [] for name in self.tracks}
        showtimes = 0
        for theater, ymd, keys, records in cgv_api.fetch_jobs(self.client, jobs):
            showtimes += len(records)
            for name in keys:
                found[name].extend(self.tracks[name].extractor(records, theater))

        alerts = []
        for name, track in self.tracks.items():
            sent = track.apply(found[name])
            if sent:
                alerts.append(f"{track.name} {sent}건")

        elapsed = time.time() - t0
        summary = " / ".join(f"{self.tracks[n].name} {len(v)}건" for n, v in found.items())
        log(f"#{self.cycle} {summary} / 슬롯 {len(jobs)}개 / 회차 {showtimes}건 / "
            f"요청 {self.client.request_count - req0}회 / {elapsed:.1f}초"
            + (f" / 🔔 알림 {', '.join(alerts)}" if alerts else ""))


def main():
    parser = argparse.ArgumentParser(description="CGV 무대인사 + IMAX/4DX 모니터 (API)")
    parser.add_argument("--once", action="store_true", help="1회만 스캔")
    parser.add_argument("--loop", type=int, default=int(os.environ.get("LOOP_SECONDS", 3000)),
                        help="루프 총 실행 시간(초)")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("LOOP_INTERVAL", 60)),
                        help="스캔 시작 간격(초)")
    parser.add_argument("--dry-run", action="store_true", help="알림을 보내지 않음")
    parser.add_argument("--track", choices=["stage", "imax", "all"], default="all",
                        help="감시 대상 선택")
    parser.add_argument("--jitter", type=int, default=0, help="시작 전 랜덤 대기 최대 초")
    args = parser.parse_args()

    if args.jitter:
        d = random.randint(0, args.jitter)
        log(f"시작 지연 {d}초")
        time.sleep(d)

    selected = ("stage", "imax") if args.track == "all" else (args.track,)
    tracks = build_tracks(dry_run=args.dry_run, selected=selected)
    monitor = Monitor(tracks, interval=args.interval)

    if args.once:
        monitor.run_cycle()
        return 0

    deadline = time.time() + args.loop
    log(f"루프 시작 - {args.loop}초 동안 {args.interval}초 간격 감시 (대상: {', '.join(selected)})")

    failures = 0
    while time.time() < deadline:
        cycle_start = time.time()
        try:
            monitor.run_cycle()
            failures = 0
            ping_healthcheck()
        except Exception as e:
            failures += 1
            log(f"사이클 실패 ({failures}회 연속): {e}")
            traceback.print_exc()
            if failures >= 3:
                log("연속 3회 실패 - 종료 (워크플로 폴백에 맡김)")
                for track in tracks.values():
                    track.notify_plain(f"⚠️ CGV 모니터 API 연속 실패로 중단: {e}")
                    break  # 같은 내용을 두 채널에 보낼 필요는 없다
                return 1
            time.sleep(30)

        # 다음 사이클까지 대기. 스캔이 interval보다 오래 걸리면 곧바로 다음 사이클.
        remaining = args.interval - (time.time() - cycle_start)
        if time.time() + max(remaining, 0) >= deadline:
            break
        if remaining > 0:
            time.sleep(remaining)

    log(f"루프 종료 - {monitor.cycle}회 스캔 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
