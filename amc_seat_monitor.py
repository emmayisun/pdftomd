#!/usr/bin/env python3
"""
AMC Seat Monitor - 监视 AMC 电影院座位取消情况
当有人取消座位、新座位变为可用时发送通知

使用方法:
    python amc_seat_monitor.py --url "https://www.amctheatres.com/..."
    python amc_seat_monitor.py --theatre-id 1234 --showtime-id 5678
    python amc_seat_monitor.py --url "..." --interval 30 --email you@example.com
"""

import argparse
import json
import re
import smtplib
import subprocess
import sys
import time
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


def parse_amc_url(url: str) -> tuple[str | None, str | None]:
    """从 AMC URL 中提取 theatre_id 和 showtime_id"""
    # 尝试从 URL 参数中解析
    # 格式: /movies/.../showtimes/all/{date}/{theatreId}?mode=...
    theatre_match = re.search(r"/showtimes/all/[\d-]+/(\d+)", url)

    # showtime ID 通常在选座页面 URL 里
    showtime_match = re.search(r"[?&]showtime[_-]?id[=:](\d+)", url, re.IGNORECASE)
    if not showtime_match:
        showtime_match = re.search(r"/showtimes?/(\d+)", url)

    theatre_id = theatre_match.group(1) if theatre_match else None
    showtime_id = showtime_match.group(1) if showtime_match else None
    return theatre_id, showtime_id


def get_seat_map(theatre_id: str, showtime_id: str) -> dict | None:
    """获取座位图数据"""
    url = f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{showtime_id}/seat-map"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        # 尝试 v1 API
        if resp.status_code == 404:
            url_v1 = f"{AMC_API_BASE}/v1/theatres/{theatre_id}/showtimes/{showtime_id}/seat-map"
            try:
                resp2 = requests.get(url_v1, headers=HEADERS, timeout=15)
                resp2.raise_for_status()
                return resp2.json()
            except Exception:
                pass
        print(f"[错误] 获取座位图失败: {e}")
        return None
    except Exception as e:
        print(f"[错误] 请求失败: {e}")
        return None


def extract_available_seats(seat_map: dict) -> set[str]:
    """从座位图中提取可用座位集合"""
    available = set()

    # AMC API 返回格式可能有多种，尽量兼容
    rows = seat_map.get("rows") or seat_map.get("_embedded", {}).get("rows", [])

    for row in rows:
        seats = row.get("seats", [])
        for seat in seats:
            status = (seat.get("status") or seat.get("seatStatus") or "").upper()
            # AVAILABLE / OPEN = 可买
            if status in ("AVAILABLE", "OPEN", "A"):
                row_id = seat.get("rowId") or seat.get("row") or row.get("rowId", "?")
                seat_num = seat.get("number") or seat.get("seatNumber") or seat.get("id", "?")
                available.add(f"{row_id}{seat_num}")

    return available


def notify_terminal(new_seats: set[str], total_available: int):
    """在终端打印通知并响铃"""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*50}")
    print(f"[{now}] 🎬 发现新可用座位！")
    print(f"  新增座位: {', '.join(sorted(new_seats))}")
    print(f"  当前共有 {total_available} 个可用座位")
    print(f"{'='*50}\n")
    # 响铃
    print("\a", end="", flush=True)


def notify_macos(new_seats: set[str], total_available: int):
    """macOS 系统通知"""
    try:
        msg = f"AMC 新座位: {', '.join(sorted(new_seats))} (共{total_available}个可用)"
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "AMC 座位监控" sound name "Glass"'
        ], check=True, capture_output=True)
    except Exception:
        pass


def notify_email(new_seats: set[str], total_available: int,
                  to_addr: str, smtp_host: str, smtp_port: int,
                  smtp_user: str, smtp_pass: str, movie_info: str):
    """发送邮件通知"""
    subject = f"AMC 座位可用提醒 - {', '.join(sorted(new_seats))}"
    body = (
        f"AMC 座位监控通知\n\n"
        f"场次: {movie_info}\n"
        f"新增可用座位: {', '.join(sorted(new_seats))}\n"
        f"当前共有 {total_available} 个可用座位\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"请尽快前往 AMC 网站购票！"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[邮件] 通知已发送至 {to_addr}")
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")


def get_showtime_info(theatre_id: str, showtime_id: str) -> str:
    """获取场次基本信息用于展示"""
    url = f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{showtime_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        movie = data.get("movieName") or data.get("movie", {}).get("name", "未知电影")
        show_time = data.get("showDateTimeLocal") or data.get("showDateTime", "")
        return f"{movie} @ {show_time}"
    except Exception:
        return f"Theatre {theatre_id} / Showtime {showtime_id}"


def monitor(theatre_id: str, showtime_id: str, interval: int = 60,
            email: str = None, smtp_host: str = "smtp.gmail.com",
            smtp_port: int = 587, smtp_user: str = None, smtp_pass: str = None):
    """主监控循环"""

    movie_info = get_showtime_info(theatre_id, showtime_id)
    print(f"\n开始监控: {movie_info}")
    print(f"Theatre ID: {theatre_id}, Showtime ID: {showtime_id}")
    print(f"检查间隔: {interval} 秒")
    print(f"按 Ctrl+C 停止\n")

    prev_available: set[str] | None = None

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        seat_map = get_seat_map(theatre_id, showtime_id)

        if seat_map is None:
            print(f"[{now}] 获取座位图失败，{interval}秒后重试...")
            time.sleep(interval)
            continue

        current_available = extract_available_seats(seat_map)

        if prev_available is None:
            # 首次检查
            print(f"[{now}] 初始状态: {len(current_available)} 个可用座位")
            if current_available:
                print(f"  可用座位: {', '.join(sorted(current_available))}")
            else:
                print("  目前无可用座位，持续监控中...")
        else:
            new_seats = current_available - prev_available
            gone_seats = prev_available - current_available

            if new_seats:
                notify_terminal(new_seats, len(current_available))
                # macOS 通知
                notify_macos(new_seats, len(current_available))
                # 邮件通知
                if email and smtp_user and smtp_pass:
                    notify_email(new_seats, len(current_available),
                                  email, smtp_host, smtp_port,
                                  smtp_user, smtp_pass, movie_info)
            elif gone_seats:
                print(f"[{now}] {len(gone_seats)} 个座位被购买，剩余 {len(current_available)} 个可用")
            else:
                print(f"[{now}] 无变化，可用座位: {len(current_available)} 个")

        prev_available = current_available
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(
        description="AMC 座位监控 - 当有人取消座位时通知你",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 直接提供 theatre ID 和 showtime ID
  python amc_seat_monitor.py --theatre-id 6238 --showtime-id 12345678

  # 从 URL 中自动解析（选座页面 URL）
  python amc_seat_monitor.py --url "https://www.amctheatres.com/movies/..."

  # 每 30 秒检查一次，并发送邮件通知（Gmail 示例）
  python amc_seat_monitor.py --theatre-id 6238 --showtime-id 12345678 \\
      --interval 30 \\
      --email yourphone@txt.att.net \\
      --smtp-user you@gmail.com \\
      --smtp-pass "your-app-password"

如何获取 Theatre ID 和 Showtime ID:
  1. 在 AMC 网站上选好场次，点击"Get Tickets"
  2. 进入选座页面
  3. 从浏览器地址栏或网络请求中找到这两个 ID
  4. 也可以在浏览器开发者工具 Network 面板中搜索 "seat-map" 请求
        """
    )

    parser.add_argument("--url", help="AMC 选座页面的 URL")
    parser.add_argument("--theatre-id", help="AMC 影院 ID")
    parser.add_argument("--showtime-id", help="场次 ID")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔（秒），默认 60")
    parser.add_argument("--email", help="通知邮件地址")
    parser.add_argument("--smtp-host", default="smtp.gmail.com", help="SMTP 服务器")
    parser.add_argument("--smtp-port", type=int, default=587, help="SMTP 端口")
    parser.add_argument("--smtp-user", help="SMTP 用户名（发件邮箱）")
    parser.add_argument("--smtp-pass", help="SMTP 密码或应用专用密码")

    args = parser.parse_args()

    theatre_id = args.theatre_id
    showtime_id = args.showtime_id

    if args.url:
        t, s = parse_amc_url(args.url)
        theatre_id = theatre_id or t
        showtime_id = showtime_id or s

    if not theatre_id or not showtime_id:
        print("错误: 需要提供 --theatre-id 和 --showtime-id，或通过 --url 自动解析")
        print("提示: 在 AMC 网站选座页面按 F12，Network 标签里搜索 'seat-map' 即可找到这两个 ID")
        parser.print_help()
        sys.exit(1)

    try:
        monitor(
            theatre_id=theatre_id,
            showtime_id=showtime_id,
            interval=args.interval,
            email=args.email,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user=args.smtp_user,
            smtp_pass=args.smtp_pass,
        )
    except KeyboardInterrupt:
        print("\n\n监控已停止。")


if __name__ == "__main__":
    main()
