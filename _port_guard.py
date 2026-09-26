"""启动前的端口守护：决定「能不能启动」以及「怎么安全释放端口」。

为什么需要它：
  2.启动代理.bat 原来是「netstat 找到 8787 的监听者就 taskkill，然后启动」。
  但桥是**单线程**的（async 端点里调同步 httpx），处理长请求时 /health 完全不回，
  端口也照样在监听。于是双击启动脚本会把一个只是"忙"的健康桥杀掉，
  客户端立刻 connection refused —— 这就是「桥老是掉」的头号成因（已定位）。

  这里把判断做成分支，**宁可不启动，也不误杀**：

    free     端口上没人监听                     -> 退出码 0，可以启动
    healthy  端口上有桥，且 /health 通过         -> 退出码 10，别动它
    busy     端口上有人在听，但 /health 不回     -> 退出码 11，默认别动（大概率在忙）；
                                                  加 --force 才允许释放

用法（给 .bat 用，也可以手动跑）：
    python _port_guard.py check [--force]   只判断，不动作
    python _port_guard.py free              只杀「精确监听该端口」的进程
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 隐藏子进程控制台窗口（netstat / taskkill），批处理链路里也不额外弹黑框。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def load_port() -> int:
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        return int(cfg.get("port") or 8787)
    except Exception:  # noqa: BLE001
        return 8787


def listener_pids(port: int) -> set[int]:
    """精确找出「正在监听这个端口」的 PID。

    只认 TCP 的 LISTENING 行，并且用**本地地址列**比端口 —— 老写法是
    `findstr :8787` 这种子串匹配，`127.0.0.1:87870` 会被误命中，进而误杀别的进程。
    """
    pids: set[int] = set()
    try:
        out = subprocess.run("netstat -ano", capture_output=True, text=True,
                             errors="replace",
                             creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:  # noqa: BLE001
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit():
            pids.add(int(parts[4]))
    return pids


def healthy(port: int, ladder=(2, 4, 8)) -> bool:
    """耐心阶梯式探活：任一档通过就算健康。

    阶梯的原因同上 —— 忙的桥 2 秒内多半不回，只探一次会把"忙"误判成"不在"。
    """
    url = f"http://127.0.0.1:{port}/health"
    for timeout in ladder:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if json.loads(r.read().decode("utf-8", "replace")).get("ok"):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def cmd_check(port: int, force: bool) -> int:
    pids = listener_pids(port)
    if not pids:
        print(f"  [free] 端口 {port} 上没有监听者，可以启动。")
        return 0
    if healthy(port):
        print(f"  [healthy] 端口 {port} 上已有健康代理（PID {sorted(pids)}），未做任何改动。")
        return 10
    if force:
        print(f"  [busy+force] 端口 {port} 被 PID {sorted(pids)} 占用且健康检查不通过，按 force 释放。")
        return 0
    print(f"  [busy] 端口 {port} 被 PID {sorted(pids)} 占用，但健康检查没通过。")
    print("         桥是单线程的，处理长请求时连 /health 都不回，所以这通常只是「忙」。")
    print("         为免误杀，这里不做任何动作。确实要换新实例请加参数：force")
    return 11


def cmd_free(port: int) -> int:
    pids = listener_pids(port)
    if not pids:
        print(f"  端口 {port} 上没有监听者。")
        return 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, text=True, errors="replace",
                           creationflags=CREATE_NO_WINDOW)
            print(f"  已结束监听 {port} 的进程 PID={pid}")
        except Exception as exc:  # noqa: BLE001
            print(f"  结束 PID={pid} 失败：{exc}")
    for _ in range(30):                       # 最多约 3 秒等端口释放
        if not listener_pids(port):
            print(f"  端口 {port} 已释放。")
            return 0
        time.sleep(0.1)
    print(f"  [警告] 端口 {port} 仍被占用：" + str(sorted(listener_pids(port))))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["check", "free"])
    ap.add_argument("--force", action="store_true",
                    help="check 时：端口被占但健康检查不通过，也允许启动（会释放端口）")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    port = args.port or load_port()
    if args.action == "check":
        return cmd_check(port, args.force)
    return cmd_free(port)


if __name__ == "__main__":
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    sys.exit(main())
