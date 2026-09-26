"""
把代理设成开机自动运行（后台静默，不弹黑框）。

两种机制，优先用第一种（不需要管理员权限）：
  1. 开始菜单「启动」文件夹里放一个快捷方式
  2. Windows 计划任务（schtasks，某些环境会禁用）

用法：
    python autostart.py            查看状态
    python autostart.py --install  设置自启并立即启动
    python autostart.py --uninstall 取消自启并停掉当前进程
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TASK = "HainnuDeepSeekProxy"
VBS = BASE / "run_hidden.vbs"
LNK = "HainnuDeepSeekProxy.lnk"

# 隐藏子进程控制台窗口：本脚本会被管理台在启动时调用（读自启状态），
# 若内部的 schtasks / powershell / where 不隐藏，开界面时就会闪黑窗。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def find_pythonw() -> Path | None:
    """定位 pythonw.exe。优先内嵌 runtime（便携，随包自足）；其次项目本地 .venv；
    找不到时再搜 PATH 上的 pyw / pythonw。"""
    embedded = BASE / "runtime" / "pythonw.exe"
    if embedded.exists():
        return embedded
    local = BASE / ".venv" / "Scripts" / "pythonw.exe"
    if local.exists():
        return local
    for name in ("pyw", "pythonw"):
        try:
            r = subprocess.run(
                ["where", name], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            for line in (r.stdout or "").splitlines():
                if line.strip():
                    return Path(line.strip())
        except Exception:  # noqa: BLE001
            continue
    return None


def write_vbs() -> Path:
    """生成静默启动代理的 VBS。内容自动定位 VBS 自身所在目录（不写死路径），
    所以整套文件夹无论拷到哪台机器、哪个路径，双击 / 开机调用都能用。"""
    # 优先用内嵌 runtime，其次 .venv：VBS 自定位目录，整套文件夹可随意搬移。
    VBS.write_text(
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
        "base = fso.GetParentFolderName(WScript.ScriptFullName)\n"
        "Set ws = CreateObject(\"WScript.Shell\")\n"
        'ws.CurrentDirectory = base\n'
        'if fso.FileExists(base & "\\runtime\\pythonw.exe") then\n'
        '  py = base & "\\runtime\\pythonw.exe"\n'
        'else\n'
        '  py = base & "\\.venv\\Scripts\\pythonw.exe"\n'
        'end if\n'
        'ws.Run """" & py & """ """ & base & "\\hainnu_proxy.py""", 0, False\n',
        encoding="ascii",
    )
    return VBS


def startup_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", str(Path.home())))
        / r"Microsoft\Windows\Start Menu\Programs\Startup"
    )


# ---------------------------------------------------------------------------
# 机制 1：启动文件夹快捷方式
# ---------------------------------------------------------------------------
def install_shortcut() -> tuple[bool, str]:
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{startup_dir() / LNK}'); "
        "$sc.TargetPath = 'wscript.exe'; "
        f"$sc.Arguments = '\"{VBS}\"'; "
        f"$sc.WorkingDirectory = '{BASE}'; "
        "$sc.Description = 'Hainnu DeepSeek proxy'; "
        "$sc.Save()"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode == 0 and shortcut_exists():
        return True, "启动文件夹快捷方式"
    return False, (r.stderr or r.stdout or "未知错误").strip()[:200]


def remove_shortcut() -> bool:
    p = startup_dir() / LNK
    try:
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        return False
    return True


def shortcut_exists() -> bool:
    return (startup_dir() / LNK).exists()


# ---------------------------------------------------------------------------
# 机制 2：计划任务（可能被安全策略禁用，失败就算了）
# ---------------------------------------------------------------------------
def task_exists() -> bool:
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", TASK],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def install_task() -> tuple[bool, str]:
    r = subprocess.run(
        [
            "schtasks", "/create", "/tn", TASK,
            "/tr", f'wscript.exe "{VBS}"',
            "/sc", "onlogon", "/rl", "limited", "/f",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode == 0:
        return True, "计划任务"
    return False, (r.stderr or r.stdout or "未知错误").strip()[:200]


def remove_task() -> bool:
    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK, "/f"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# 进程管理
# ---------------------------------------------------------------------------
def start_now() -> bool:
    """立刻在后台拉起代理（用 VBS，不弹窗）。"""
    subprocess.Popen(
        ["wscript.exe", str(VBS)],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    for _ in range(20):
        time.sleep(1)
        if port_alive():
            return True
    return False


def stop_now() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*hainnu_proxy*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        pass


def port_alive(port: int = 8787) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def do_install() -> int:
    pyw = find_pythonw()
    if pyw is None:
        print("[错误] 找不到 pythonw.exe，请先运行 0.安装依赖.bat")
        return 1
    print(f"解释器    : {pyw}")
    vbs = write_vbs()
    print(f"启动脚本  : {vbs}")

    ok, how = install_shortcut()
    if ok:
        print(f"[完成] 已设置开机自启（{how}）")
    else:
        print(f"[提示] 启动文件夹方式失败：{how}")
        ok2, how2 = install_task()
        if ok2:
            print(f"[完成] 已设置开机自启（{how2}）")
        else:
            print(f"[错误] 两种方式都失败：{how2}")
            print("       请右键「此电脑」→ 管理，或联系管理员放行 schtasks。")
            return 1

    if port_alive():
        print("[提示] 代理已在运行，跳过启动")
    else:
        print("正在后台启动…")
        if start_now():
            print("[完成] 代理已运行: http://127.0.0.1:8787/v1")
        else:
            print("[提示] 已写入自启，但当前没起来。看 hainnu_proxy.log 排查。")
    return 0


def do_uninstall() -> int:
    remove_shortcut()
    if task_exists():
        remove_task()
    print("[完成] 已取消开机自启")
    stop_now()
    print("[完成] 已停止运行中的代理")
    return 0


def do_status() -> int:
    sc = shortcut_exists()
    tk = task_exists()
    alive = port_alive()
    print(f"自启(快捷方式) : {'已设置' if sc else '未设置'}")
    print(f"自启(计划任务) : {'已注册' if tk else '未注册'}")
    print(f"代理进程       : {'运行中' if alive else '未运行'}")
    if alive:
        print("接口地址       : http://127.0.0.1:8787/v1")
    log = BASE / "hainnu_proxy.log"
    print(f"日志文件       : {log} {'(存在)' if log.exists() else '(无)'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="设置开机自启并立即启动")
    g.add_argument("--uninstall", action="store_true", help="取消自启并停止当前进程")
    g.add_argument("--status", action="store_true", help="查看状态（默认）")
    args = ap.parse_args()
    if args.install:
        return do_install()
    if args.uninstall:
        return do_uninstall()
    return do_status()


if __name__ == "__main__":
    sys.exit(main())
