#!/usr/bin/env python3
"""
AMC Seat Monitor - 监视 AMC 电影院座位取消情况
支持同时监测多个场次，当有人取消座位时发送通知

使用方法:
  单场次:
    python amc_seat_monitor.py --theatre-id 6238 --showtime-id 12345678

  多场次（用配置文件）:
    python amc_seat_monitor.py --config showtimes.json

  多场次（命令行）:
    python amc_seat_monitor.py \
        --showtime 6238:11111111 \
        --showtime 6238:22222222 \
        --showtime 6238:33333333
"""

import argparse
import json
import re
import smtplib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText

import requests

# AMC API 配置
AMC_API_BASE = "https://api.amctheatres.com"
AMC_API_KEY = "awjFNSdBBHyMPFKSPvk4jFAmRiHRtIw0"  # 公开的 AMC web API key

HEADERS = {
    "X-AMC-Vendor-Key": AMC_API_KEY,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

# 全局打印锁，防止多线程输出混乱
_print_lock = threading.Lock()


def tprint(*args, **kwargs):
    """线程安全的 print"""
    with _print_lock:
        print(*args, **kwargs)


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class ShowtimeConfig:
    theatre_id: str
    showtime_id: str
    label: str = ""          # 可选的自定义名称，如 "周六下午场"

    def display_name(self) -> str:
        if self.label:
            return f"{self.label} (T:{self.theatre_id}/S:{self.showtime_id})"
        return f"Theatre {self.theatre_id} / Showtime {self.showtime_id}"


@dataclass
class NotifyConfig:
    email: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""


# ─── AMC API ─────────────────────────────────────────────────────────────────

def get_seat_map(theatre_id: str, showtime_id: str) -> dict | None:
    """获取座位图数据"""
    for version in ("v2", "v1"):
        url = f"{AMC_API_BASE}/{version}/theatres/{theatre_id}/showtimes/{showtime_id}/seat-map"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            tprint(f"[错误] {theatre_id}/{showtime_id} 座位图请求失败: {e}")
            return None
        except Exception as e:
            tprint(f"[错误] {theatre_id}/{showtime_id} 请求异常: {e}")
            return None
    return None


def extract_available_seats(seat_map: dict) -> set[str]:
    """从座位图中提取可用座位集合"""
    available = set()
    rows = seat_map.get("rows") or seat_map.get("_embedded", {}).get("rows", [])
    for row in rows:
        for seat in row.get("seats", []):
            status = (seat.get("status") or seat.get("seatStatus") or "").upper()
            if status in ("AVAILABLE", "OPEN", "A"):
                row_id = seat.get("rowId") or seat.get("row") or row.get("rowId", "?")
                seat_num = seat.get("number") or seat.get("seatNumber") or seat.get("id", "?")
                available.add(f"{row_id}{seat_num}")
    return available


def get_showtime_info(theatre_id: str, showtime_id: str) -> str:
    """获取场次基本信息"""
    url = f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{showtime_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        movie = data.get("movieName") or data.get("movie", {}).get("name", "未知电影")
        show_time = data.get("showDateTimeLocal") or data.get("showDateTime", "")
        return f"{movie} @ {show_time}"
    except Exception:
        return f"Theatre {theatre_id} / Showtime {showtime_id}"


# ─── 通知 ─────────────────────────────────────────────────────────────────────

def notify_terminal(label: str, new_seats: set[str], total: int):
    now = datetime.now().strftime("%H:%M:%S")
    tprint(f"\n{'='*55}")
    tprint(f"[{now}] 发现新可用座位！  {label}")
    tprint(f"  新增座位: {', '.join(sorted(new_seats))}")
    tprint(f"  当前共有 {total} 个可用座位")
    tprint(f"{'='*55}\n")
    print("\a", end="", flush=True)


def notify_macos(label: str, new_seats: set[str], total: int):
    try:
        msg = f"{label}: 新座位 {', '.join(sorted(new_seats))} (共{total}个)"
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "AMC 座位监控" sound name "Glass"'],
            check=True, capture_output=True,
        )
    except Exception:
        pass


def notify_email(label: str, new_seats: set[str], total: int, cfg: NotifyConfig):
    if not (cfg.email and cfg.smtp_user and cfg.smtp_pass):
        return
    subject = f"[AMC] {label} 有新座位: {', '.join(sorted(new_seats))}"
    body = (
        f"AMC 座位监控通知\n\n"
        f"场次: {label}\n"
        f"新增可用座位: {', '.join(sorted(new_seats))}\n"
        f"当前共有 {total} 个可用座位\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"请尽快前往 AMC 网站购票！"
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
        tprint(f"[邮件] 已通知 {cfg.email}")
    except Exception as e:
        tprint(f"[邮件] 发送失败: {e}")


# ─── 单场次监控线程 ────────────────────────────────────────────────────────────

def monitor_one(showtime: ShowtimeConfig, interval: int, notify_cfg: NotifyConfig,
                stop_event: threading.Event):
    """监控单个场次，在独立线程中运行"""
    info = get_showtime_info(showtime.theatre_id, showtime.showtime_id)
    label = showtime.label or info
    tprint(f"[启动] {label}")

    prev_available: set[str] | None = None

    while not stop_event.is_set():
        now = datetime.now().strftime("%H:%M:%S")
        seat_map = get_seat_map(showtime.theatre_id, showtime.showtime_id)

        if seat_map is None:
            tprint(f"[{now}] [{label}] 获取失败，{interval}s 后重试")
            stop_event.wait(interval)
            continue

        current = extract_available_seats(seat_map)

        if prev_available is None:
            tprint(f"[{now}] [{label}] 初始: {len(current)} 个可用座位"
                   + (f" — {', '.join(sorted(current))}" if current else " — 已售罄，持续监控"))
        else:
            new_seats = current - prev_available
            gone = prev_available - current
            if new_seats:
                notify_terminal(label, new_seats, len(current))
                notify_macos(label, new_seats, len(current))
                notify_email(label, new_seats, len(current), notify_cfg)
            elif gone:
                tprint(f"[{now}] [{label}] {len(gone)} 座被购，剩 {len(current)} 可用")
            else:
                tprint(f"[{now}] [{label}] 无变化，可用: {len(current)}")

        prev_available = current
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
    """
    加载 JSON 配置文件，格式示例:
    [
      {"theatre_id": "6238", "showtime_id": "11111111", "label": "周六 10am"},
      {"theatre_id": "6238", "showtime_id": "22222222", "label": "周六 2pm"},
      {"theatre_id": "6238", "showtime_id": "33333333"}
    ]
    """
    with open(path) as f:
        data = json.load(f)
    showtimes = []
    for item in data:
        showtimes.append(ShowtimeConfig(
            theatre_id=str(item["theatre_id"]),
            showtime_id=str(item["showtime_id"]),
            label=item.get("label", ""),
        ))
    return showtimes


# ─── 主函数 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AMC 座位监控 - 同时监测多个场次，有人取消时通知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单场次
  python amc_seat_monitor.py --theatre-id 6238 --showtime-id 12345678

  # 多场次（命令行）
  python amc_seat_monitor.py \\
      --showtime 6238:11111111:"工作日6pm场" \\
      --showtime 6238:22222222:"周六上午场" \\
      --showtime 6238:33333333:"周六下午场" \\
      --showtime 6238:44444444:"周日全天场"

  # 多场次（JSON 配置文件，推荐）
  python amc_seat_monitor.py --config showtimes.json --interval 30

  # 加邮件通知
  python amc_seat_monitor.py --config showtimes.json \\
      --email you@example.com \\
      --smtp-user you@gmail.com \\
      --smtp-pass "xxxx xxxx xxxx xxxx"

showtimes.json 格式:
  [
    {"theatre_id": "6238", "showtime_id": "11111111", "label": "工作日6pm"},
    {"theatre_id": "6238", "showtime_id": "22222222", "label": "周六10am"},
    {"theatre_id": "6238", "showtime_id": "33333333", "label": "周六2pm"},
    {"theatre_id": "6238", "showtime_id": "44444444", "label": "周日全天"}
  ]

如何找到 Theatre ID 和 Showtime ID:
  在 AMC 选座页面按 F12 -> Network -> 搜索 "seat-map"
  URL 格式: /v2/theatres/{theatreId}/showtimes/{showtimeId}/seat-map
        """
    )

    # 输入方式
    parser.add_argument("--config", help="JSON 配置文件路径（多场次推荐）")
    parser.add_argument("--showtime", action="append", metavar="THEATRE:SHOWTIME[:LABEL]",
                        help="场次，格式 theatreId:showtimeId 或 theatreId:showtimeId:标签，可重复")
    parser.add_argument("--theatre-id", help="单场次影院 ID")
    parser.add_argument("--showtime-id", help="单场次场次 ID")
    parser.add_argument("--url", help="AMC 选座页面 URL（自动解析 ID）")
    parser.add_argument("--label", default="", help="单场次的自定义名称")

    # 监控设置
    parser.add_argument("--interval", type=int, default=60, help="检查间隔（秒），默认 60")

    # 通知设置
    parser.add_argument("--email", help="通知收件地址")
    parser.add_argument("--smtp-host", default="smtp.gmail.com")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-user", help="发件邮箱")
    parser.add_argument("--smtp-pass", help="邮箱密码或应用专用密码")

    args = parser.parse_args()

    # ── 收集所有场次 ──
    showtimes: list[ShowtimeConfig] = []

    if args.config:
        showtimes.extend(load_config_file(args.config))

    if args.showtime:
        for s in args.showtime:
            parts = s.split(":", 2)
            if len(parts) < 2:
                print(f"[错误] --showtime 格式应为 theatreId:showtimeId，收到: {s}")
                sys.exit(1)
            showtimes.append(ShowtimeConfig(
                theatre_id=parts[0],
                showtime_id=parts[1],
                label=parts[2] if len(parts) > 2 else "",
            ))

    # 单场次参数
    theatre_id = args.theatre_id
    showtime_id = args.showtime_id
    if args.url:
        t, s = parse_amc_url(args.url)
        theatre_id = theatre_id or t
        showtime_id = showtime_id or s
    if theatre_id and showtime_id:
        showtimes.append(ShowtimeConfig(theatre_id, showtime_id, args.label))

    if not showtimes:
        print("错误: 请至少提供一个场次（--showtime / --config / --theatre-id+--showtime-id）")
        parser.print_help()
        sys.exit(1)

    # ── 通知配置 ──
    notify_cfg = NotifyConfig(
        email=args.email or "",
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user or "",
        smtp_pass=args.smtp_pass or "",
    )

    # ── 启动多线程监控 ──
    print(f"\n共监控 {len(showtimes)} 个场次，检查间隔 {args.interval} 秒，按 Ctrl+C 停止\n")
    stop_event = threading.Event()
    threads = []

    for showtime in showtimes:
        t = threading.Thread(
            target=monitor_one,
            args=(showtime, args.interval, notify_cfg, stop_event),
            daemon=True,
            name=f"monitor-{showtime.showtime_id}",
        )
        t.start()
        threads.append(t)
        time.sleep(0.5)  # 错开启动，避免同时请求

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n正在停止所有监控线程...")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
        print("已停止。")


if __name__ == "__main__":
    main()
