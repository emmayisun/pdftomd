#!/usr/bin/env python3
"""
AMC Seat Monitor - 监视 AMC 电影院座位取消情况
支持同时监测多个场次，只在指定排有相邻两座时通知

使用方法:
  python amc_seat_monitor.py --config showtimes.json --interval 30
"""

import argparse
import json
import re
import smtplib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText

import requests

AMC_API_BASE = "https://api.amctheatres.com"
AMC_API_KEY = "awjFNSdBBHyMPFKSPvk4jFAmRiHRtIw0"

HEADERS = {
    "X-AMC-Vendor-Key": AMC_API_KEY,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

_print_lock = threading.Lock()


def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ─── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class SeatFilter:
    """座位过滤条件"""
    rows: set[str]        # 只关注这些排，如 {"M","L","K","J","H","G","F"}
    need_adjacent: bool   # 是否要求两个相邻座位同时出现
    min_count: int = 2    # 同排最少需要几个座位

    @staticmethod
    def default() -> "SeatFilter":
        return SeatFilter(rows=set(), need_adjacent=False, min_count=1)


@dataclass
class ShowtimeConfig:
    showtime_id: str
    theatre_id: str = ""
    label: str = ""

    def display_name(self) -> str:
        if self.label:
            return f"{self.label} ({self.showtime_id})"
        return f"Showtime {self.showtime_id}"


@dataclass
class NotifyConfig:
    email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""


# ─── AMC API ──────────────────────────────────────────────────────────────────

def get_seat_map(theatre_id: str | None, showtime_id: str) -> dict | None:
    candidates = [
        f"{AMC_API_BASE}/v2/showtimes/{showtime_id}/seat-map",
        f"{AMC_API_BASE}/v1/showtimes/{showtime_id}/seat-map",
    ]
    if theatre_id:
        candidates += [
            f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{showtime_id}/seat-map",
            f"{AMC_API_BASE}/v1/theatres/{theatre_id}/showtimes/{showtime_id}/seat-map",
        ]
    for url in candidates:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            tprint(f"[错误] {showtime_id} 请求失败: {e}")
            return None
        except Exception as e:
            tprint(f"[错误] {showtime_id} 请求异常: {e}")
            return None
    return None


def get_showtime_info(theatre_id: str | None, showtime_id: str) -> str:
    for url in [f"{AMC_API_BASE}/v2/showtimes/{showtime_id}"] + (
        [f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{showtime_id}"] if theatre_id else []
    ):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 404:
                continue
            data = resp.json()
            movie = data.get("movieName") or data.get("movie", {}).get("name", "未知电影")
            show_time = data.get("showDateTimeLocal") or data.get("showDateTime", "")
            return f"{movie} @ {show_time}"
        except Exception:
            continue
    return f"Showtime {showtime_id}"


# ─── 座位解析与过滤 ───────────────────────────────────────────────────────────

def extract_available_seats(seat_map: dict) -> dict[str, list[int]]:
    """
    返回 {row: [seat_numbers...]} 的字典，只包含可用座位。
    seat_numbers 已排序。
    """
    rows_data: dict[str, list[int]] = {}
    rows = seat_map.get("rows") or seat_map.get("_embedded", {}).get("rows", [])
    for row in rows:
        for seat in row.get("seats", []):
            status = (seat.get("status") or seat.get("seatStatus") or "").upper()
            if status in ("AVAILABLE", "OPEN", "A"):
                row_id = (
                    seat.get("rowId") or seat.get("row") or row.get("rowId", "?")
                ).upper().strip()
                seat_num_raw = seat.get("number") or seat.get("seatNumber") or seat.get("id")
                try:
                    seat_num = int(seat_num_raw)
                except (TypeError, ValueError):
                    continue
                rows_data.setdefault(row_id, []).append(seat_num)
    for row_id in rows_data:
        rows_data[row_id].sort()
    return rows_data


def find_adjacent_pairs(seat_nums: list[int]) -> list[tuple[int, int]]:
    """在已排序的座位号列表中找出所有相邻对"""
    pairs = []
    for i in range(len(seat_nums) - 1):
        if seat_nums[i + 1] == seat_nums[i] + 1:
            pairs.append((seat_nums[i], seat_nums[i + 1]))
    return pairs


def check_filter(available: dict[str, list[int]], seat_filter: SeatFilter) -> list[str]:
    """
    检查当前可用座位是否满足过滤条件。
    返回满足条件的座位描述列表（空列表 = 不满足）。
    """
    results = []
    target_rows = {r.upper() for r in seat_filter.rows} if seat_filter.rows else set(available.keys())

    for row_id, nums in available.items():
        if row_id not in target_rows:
            continue

        if seat_filter.need_adjacent:
            pairs = find_adjacent_pairs(nums)
            for a, b in pairs:
                results.append(f"{row_id}{a}+{row_id}{b}")
        else:
            if len(nums) >= seat_filter.min_count:
                results.extend(f"{row_id}{n}" for n in nums)

    return results


def seats_to_flat_set(available: dict[str, list[int]]) -> set[str]:
    """把行列字典转成 flat set 用于比较变化"""
    result = set()
    for row_id, nums in available.items():
        for n in nums:
            result.add(f"{row_id}{n}")
    return result


# ─── 通知 ─────────────────────────────────────────────────────────────────────

def notify_terminal(label: str, matches: list[str], showtime_id: str):
    now = datetime.now().strftime("%H:%M:%S")
    tprint(f"\n{'='*60}")
    tprint(f"[{now}]  找到符合条件的座位！")
    tprint(f"  场次: {label}")
    tprint(f"  座位: {', '.join(matches)}")
    tprint(f"  立即购票: https://www.amctheatres.com/showtimes/{showtime_id}/seats")
    tprint(f"{'='*60}\n")
    print("\a", end="", flush=True)


def notify_macos(label: str, matches: list[str], showtime_id: str):
    try:
        msg = f"{label}: {', '.join(matches)}"
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "AMC 座位可抢！" sound name "Glass"'],
            check=True, capture_output=True,
        )
    except Exception:
        pass


def notify_email(label: str, matches: list[str], showtime_id: str, cfg: NotifyConfig):
    if not (cfg.email and cfg.smtp_user and cfg.smtp_pass):
        return
    subject = f"[AMC] {label} 有座位可抢: {', '.join(matches)}"
    body = (
        f"AMC 座位监控提醒\n\n"
        f"场次: {label}\n"
        f"符合条件的座位: {', '.join(matches)}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"立即购票:\n"
        f"https://www.amctheatres.com/showtimes/{showtime_id}/seats\n\n"
        f"请尽快操作，座位可能很快被抢走！"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.email
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
            server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.send_message(msg)
        tprint(f"[邮件] 已发送至 {cfg.email}")
    except Exception as e:
        tprint(f"[邮件] 发送失败: {e}")


# ─── 单场次监控线程 ────────────────────────────────────────────────────────────

def monitor_one(showtime: ShowtimeConfig, seat_filter: SeatFilter, interval: int,
                notify_cfg: NotifyConfig, stop_event: threading.Event):
    theatre_id = showtime.theatre_id or None
    info = get_showtime_info(theatre_id, showtime.showtime_id)
    label = showtime.label or info
    tprint(f"[启动] {label}")

    prev_flat: set[str] | None = None
    was_notified = False  # 避免重复通知同一状态

    while not stop_event.is_set():
        now = datetime.now().strftime("%H:%M:%S")
        seat_map = get_seat_map(theatre_id, showtime.showtime_id)

        if seat_map is None:
            tprint(f"[{now}] [{label}] 获取失败，{interval}s 后重试")
            stop_event.wait(interval)
            continue

        available = extract_available_seats(seat_map)
        current_flat = seats_to_flat_set(available)
        matches = check_filter(available, seat_filter)

        if prev_flat is None:
            # 首次检查
            filter_desc = (
                f"目标排: {', '.join(sorted(seat_filter.rows))}" if seat_filter.rows
                else "所有排"
            )
            cond_desc = "需要相邻两座" if seat_filter.need_adjacent else f"至少{seat_filter.min_count}座"
            if matches:
                tprint(f"[{now}] [{label}] 初始即满足条件！{filter_desc} / {cond_desc}")
                notify_terminal(label, matches, showtime.showtime_id)
                notify_macos(label, matches, showtime.showtime_id)
                notify_email(label, matches, showtime.showtime_id, notify_cfg)
                was_notified = True
            else:
                # 显示目标排当前状态
                target_rows = {r.upper() for r in seat_filter.rows} if seat_filter.rows else set(available.keys())
                row_status = []
                for r in sorted(target_rows):
                    cnt = len(available.get(r, []))
                    row_status.append(f"{r}排:{cnt}座")
                tprint(f"[{now}] [{label}] 暂无符合条件座位 ({filter_desc} / {cond_desc})"
                       + (f" — {' '.join(row_status)}" if row_status else ""))
        else:
            changed = current_flat != prev_flat
            if matches:
                if not was_notified or changed:
                    notify_terminal(label, matches, showtime.showtime_id)
                    notify_macos(label, matches, showtime.showtime_id)
                    notify_email(label, matches, showtime.showtime_id, notify_cfg)
                    was_notified = True
                else:
                    # 仍然满足，静默
                    target_rows = {r.upper() for r in seat_filter.rows} if seat_filter.rows else set(available.keys())
                    row_status = [f"{r}:{len(available.get(r,[]))}座" for r in sorted(target_rows) if available.get(r)]
                    tprint(f"[{now}] [{label}] 仍有符合条件座位: {', '.join(matches[:3])}{'…' if len(matches)>3 else ''}")
            else:
                if was_notified:
                    tprint(f"[{now}] [{label}] 座位已被抢走，继续监控...")
                    was_notified = False
                elif changed:
                    target_rows = {r.upper() for r in seat_filter.rows} if seat_filter.rows else set(available.keys())
                    row_status = []
                    for r in sorted(target_rows):
                        cnt = len(available.get(r, []))
                        row_status.append(f"{r}:{cnt}")
                    tprint(f"[{now}] [{label}] 有变化但不满足条件 — {' '.join(row_status)}")
                else:
                    target_rows = {r.upper() for r in seat_filter.rows} if seat_filter.rows else set(available.keys())
                    row_status = []
                    for r in sorted(target_rows):
                        cnt = len(available.get(r, []))
                        row_status.append(f"{r}:{cnt}")
                    tprint(f"[{now}] [{label}] 无变化 — {' '.join(row_status)}")

        prev_flat = current_flat
        stop_event.wait(interval)


# ─── 解析输入 ─────────────────────────────────────────────────────────────────

def parse_amc_url(url: str) -> tuple[str | None, str | None]:
    theatre_match = re.search(r"/showtimes/all/[\d-]+/(\d+)", url)
    showtime_match = re.search(r"[?&]showtime[_-]?id[=:](\d+)", url, re.IGNORECASE)
    if not showtime_match:
        showtime_match = re.search(r"/showtimes?/(\d+)", url)
    return (
        theatre_match.group(1) if theatre_match else None,
        showtime_match.group(1) if showtime_match else None,
    )


def load_config_file(path: str) -> list[ShowtimeConfig]:
    with open(path) as f:
        data = json.load(f)
    return [
        ShowtimeConfig(
            showtime_id=str(item["showtime_id"]),
            theatre_id=str(item.get("theatre_id", "")),
            label=item.get("label", ""),
        )
        for item in data
    ]


# ─── 主函数 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AMC 座位监控 - 指定排，同时有两个相邻座位时通知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 监控指定场次，M/L/K/J/H/G/F 排，必须有相邻两座
  python amc_seat_monitor.py --config showtimes.json \\
      --rows M,L,K,J,H,G,F --adjacent --interval 30

  # 不限排，只要有任意两个相邻座就通知
  python amc_seat_monitor.py --config showtimes.json --adjacent

  # 单场次命令行
  python amc_seat_monitor.py --showtime-id 140796603 \\
      --rows M,L,K,J,H,G,F --adjacent

  # 加邮件通知
  python amc_seat_monitor.py --config showtimes.json \\
      --rows M,L,K,J,H,G,F --adjacent \\
      --email you@example.com \\
      --smtp-user you@gmail.com \\
      --smtp-pass "xxxx xxxx xxxx xxxx"

showtimes.json 格式:
  [
    {"showtime_id": "140796603", "label": "周六10am"},
    {"showtime_id": "140796597", "label": "周六1pm"},
    {"showtime_id": "140796596", "label": "周六4pm"}
  ]
        """
    )

    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--showtime", action="append", metavar="ID[:LABEL]",
                        help="场次 ID，可重复")
    parser.add_argument("--showtime-id", help="单场次 ID")
    parser.add_argument("--theatre-id", help="影院 ID（通常不需要）")
    parser.add_argument("--url", help="AMC 选座页面 URL")
    parser.add_argument("--label", default="", help="单场次名称")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔（秒），默认 60")

    # 座位过滤
    parser.add_argument("--rows", default="",
                        help="只监控这些排，逗号分隔，如 M,L,K,J,H,G,F（不填=所有排）")
    parser.add_argument("--adjacent", action="store_true",
                        help="只在同一排有至少两个相邻座位时通知")
    parser.add_argument("--min-seats", type=int, default=2,
                        help="同排最少几个座位（--adjacent 未开启时生效），默认 2")

    # 通知
    parser.add_argument("--email", help="通知收件地址")
    parser.add_argument("--smtp-host", default="smtp.gmail.com")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-user", help="发件邮箱")
    parser.add_argument("--smtp-pass", help="邮箱密码")

    args = parser.parse_args()

    # ── 收集场次 ──
    showtimes: list[ShowtimeConfig] = []

    if args.config:
        showtimes.extend(load_config_file(args.config))

    if args.showtime:
        for s in args.showtime:
            parts = s.split(":", 1)
            showtimes.append(ShowtimeConfig(showtime_id=parts[0],
                                             label=parts[1] if len(parts) > 1 else ""))

    showtime_id = args.showtime_id
    theatre_id = args.theatre_id
    if args.url:
        t, s = parse_amc_url(args.url)
        theatre_id = theatre_id or t
        showtime_id = showtime_id or s
    if showtime_id:
        showtimes.append(ShowtimeConfig(showtime_id=showtime_id,
                                         theatre_id=theatre_id or "",
                                         label=args.label))

    if not showtimes:
        print("错误: 请至少提供一个场次（--showtime-id / --showtime / --config）")
        parser.print_help()
        sys.exit(1)

    # ── 座位过滤条件 ──
    rows = {r.strip().upper() for r in args.rows.split(",") if r.strip()} if args.rows else set()
    seat_filter = SeatFilter(
        rows=rows,
        need_adjacent=args.adjacent,
        min_count=args.min_seats,
    )

    # ── 通知配置 ──
    notify_cfg = NotifyConfig(
        email=args.email or "",
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user or "",
        smtp_pass=args.smtp_pass or "",
    )

    # ── 打印启动信息 ──
    rows_desc = f"目标排: {', '.join(sorted(rows))}" if rows else "所有排"
    cond_desc = "需要相邻两座" if args.adjacent else f"至少{args.min_seats}座"
    print(f"\n监控 {len(showtimes)} 个场次 | {rows_desc} | {cond_desc} | 间隔 {args.interval}s")
    print("按 Ctrl+C 停止\n")

    # ── 启动线程 ──
    stop_event = threading.Event()
    threads = []

    for showtime in showtimes:
        t = threading.Thread(
            target=monitor_one,
            args=(showtime, seat_filter, args.interval, notify_cfg, stop_event),
            daemon=True,
            name=f"monitor-{showtime.showtime_id}",
        )
        t.start()
        threads.append(t)
        time.sleep(0.3)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n正在停止...")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
        print("已停止。")


if __name__ == "__main__":
    main()
