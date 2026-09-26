"""检查（并在需要时安装）本地服务运行所需的依赖。

只用标准库，因此**任何** Python 都能跑它 —— 包括还没装依赖的系统 Python。
这点是刻意的：服务本体需要 fastapi/uvicorn/httpx，若本脚本也依赖它们，
就没人能在"缺依赖"的状态下完成检查与修复。

核心原则：**能复用就不下载。**

  1. 先把本机找得到的解释器都探一遍 —— 便携运行时、项目 .venv、`py -0p` 列出的
     全部已安装版本、官方安装器目录、conda/miniforge 及其各 env、PATH 上的
     python —— 谁已经装齐 fastapi+uvicorn+httpx 就直接用谁，**零下载**；
  2. 一个都没有才建 .venv，且带 `--system-site-packages`：让 venv 直接看见
     系统里已经装好的包，缺几个补几个，而不是整套重下一遍；
  3. 真要装时 pip 源按 清华 → 阿里 → 官方 依次回退。实测本机 pip 访问清华源
     会被 403（curl 请求同一 URL 却是 200），镜像的封锁是"挑客户端"的，
     写死单一源会让部分机器怎么装都装不上。

用法：
    python _deps_check.py                仅检查
    python _deps_check.py --install      检查；本机都没有则建 .venv 并装
    python _deps_check.py --print-python 把应使用的解释器输出成 PY=<路径>

输出约定：**stdout 只放机器读的 `PY=` 行**，给人看的进度与提示一律走 stderr。
这样 `2.启动代理.bat` 可以把 stdout 重定向到文件再取路径，同时窗口里照常滚动
安装进度。（Python 在 Windows 控制台用宽字符写，中文不会乱码。）

退出码：
    0   有可用解释器（依赖齐全）
    10  本机没有任何带依赖的解释器（未带 --install）
    11  尝试安装但失败
    12  本机找不到任何可用的 Python
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
REQS = BASE / "requirements.txt"

PIP_INDEXES = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]

OK, MISSING, INSTALL_FAILED, NO_PYTHON = 0, 10, 11, 12

DEPS = ("fastapi", "uvicorn", "httpx")
MAX_PROBE = 15          # 最多探这么多个解释器：conda 环境多时别把人等老

# 隐藏子进程控制台窗口：管理台（无控制台的窗口进程）会在启动/起代理前调用本脚本，
# 若内部的探测/pip 子进程不隐藏，用户就会看到一闪而过的黑框。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def log(msg: str) -> None:
    """给人看的一律走 stderr，别污染 stdout 的 PY= 行。"""
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- 候选解释器

def _dedup(paths) -> list[Path]:
    out, seen = [], set()
    for p in paths:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _py_launcher_pythons() -> list[Path]:
    """`py -0p` 会列出本机所有已安装版本及其完整路径，比猜目录可靠得多。"""
    exe = shutil.which("py")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-0p"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        m = re.search(r"([A-Za-z]:\\.*python\.exe)\s*$", line.strip())
        if m:
            out.append(Path(m.group(1)))
    return out


def _installed_pythons() -> list[Path]:
    """官方安装器的位置：%LOCALAPPDATA%\\Programs\\Python\\Python3xx\\python.exe"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    d = Path(base) / "Programs" / "Python"
    if not d.is_dir():
        return []
    out = []
    try:
        # 版本号大的排前面：新版更可能带着我们要的包
        for sub in sorted(d.iterdir(), reverse=True):
            p = sub / "python.exe"
            if p.exists():
                out.append(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def _conda_pythons() -> list[Path]:
    """conda / miniforge 的 base 与各 env —— 很多人把包装在这里而不是系统 Python。"""
    home = Path.home()
    names = ("miniconda3", "anaconda3", "miniforge3")
    roots = [home / n for n in names]
    roots += [home / "AppData" / "Local" / n for n in names]
    roots += [Path("C:/ProgramData") / n for n in names]
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        if (root / "python.exe").exists():
            out.append(root / "python.exe")
        envs = root / "envs"
        if not envs.is_dir():
            continue
        try:
            for d in sorted(envs.iterdir()):
                p = d / "python.exe"
                if p.exists():
                    out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def candidate_pythons() -> list[Path]:
    """本机所有值得一试的解释器，按"越可能自带依赖越靠前"排序。"""
    cands = []
    for base in (BASE / "runtime", BASE / ".venv" / "Scripts"):
        for name in ("python.exe", "pythonw.exe"):
            cands.append(base / name)
    cands.append(Path(sys.executable))
    cands += _py_launcher_pythons()
    for name in ("python", "python3", "py"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    cands += _installed_pythons()
    cands += _conda_pythons()
    return _dedup(cands)


def has_deps(py: Path) -> bool:
    try:
        return subprocess.run(
            [str(py), "-c", "import " + ",".join(DEPS)],
            capture_output=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def resolve(verbose: bool = False) -> Path | None:
    """本机第一个"依赖齐全"的解释器 —— 找到就意味着一个包都不用下。"""
    n = 0
    for py in candidate_pythons():
        if n >= MAX_PROBE:
            break
        try:
            if not py.exists():
                continue
        except Exception:  # noqa: BLE001
            continue
        n += 1
        if verbose:
            log(f"  探测 {py} ...")
        if has_deps(py):
            return py
    return None


# ---------------------------------------------------------------- 安装

def venv_python() -> Path | None:
    for name in ("python.exe", "pythonw.exe"):
        p = BASE / ".venv" / "Scripts" / name
        if p.exists():
            return p
    return None


def dep_hits(py: Path) -> int:
    """该解释器已经装了几个目标依赖 —— 一次进程问完，别一个包起一次。"""
    code = ("import importlib.util,sys;"
            "print(sum(1 for m in sys.argv[1:] "
            "if importlib.util.find_spec(m) is not None))")
    try:
        r = subprocess.run([str(py), "-c", code, *DEPS],
                           capture_output=True, text=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        return int((r.stdout or "0").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def pick_base() -> tuple[Path | None, int]:
    """挑 venv 的基座解释器：已装依赖越多越好。

    配合 --system-site-packages，基座已有的包会被新建的 venv 直接看见，
    pip 就不会把它们再下一遍 —— 这是"能找到就不重复下载"的第二层。
    """
    best, best_hits = None, -1
    for py in candidate_pythons()[:8]:
        s = str(py).lower()
        if "runtime" in s or ".venv" in s:      # 别拿我们自己造的环境当基座
            continue
        try:
            if not py.exists():
                continue
            if subprocess.run([str(py), "-c", "pass"],
                              capture_output=True, timeout=30,
                              creationflags=CREATE_NO_WINDOW).returncode != 0:
                continue
        except Exception:  # noqa: BLE001
            continue
        hits = dep_hits(py)
        if hits > best_hits:
            best, best_hits = py, hits
        if hits == len(DEPS):
            break
    if best is None:
        return system_python(), 0
    return best, max(best_hits, 0)


def system_python() -> Path | None:
    """随便找一个能用的解释器 —— 不要求它装了依赖。"""
    cands = [Path(sys.executable)]
    for name in ("py", "python", "python3"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        try:
            if c.exists() and subprocess.run(
                [str(c), "-c", "pass"], capture_output=True, timeout=30,
                creationflags=CREATE_NO_WINDOW,
            ).returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


def run(cmd: list[str], timeout: int = 900, live: bool = False,
        to_stderr: bool = False) -> subprocess.CompletedProcess:
    """live=True 时输出直通控制台。

    to_stderr=True 把子进程 stdout 接到本进程 stderr —— bat 会把我们的 stdout
    重定向到文件去取 PY= 行，进度信息必须绕开 stdout 才能被用户看见。
    """
    if live:
        return subprocess.run(cmd, timeout=timeout,
                              stdout=(sys.stderr if to_stderr else None),
                              creationflags=CREATE_NO_WINDOW)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def install() -> int:
    """建 .venv（如需要）并装依赖。

    装到 .venv 而非系统 Python：不污染系统环境，且与 _find_python.bat 的探测
    顺序（runtime → .venv → 系统）一致。带 --system-site-packages 是为了让
    venv 直接看见系统里已有的包 —— 已有的不再下一遍。
    """
    vpy = venv_python()
    if vpy is None:
        syspy, hits = pick_base()
        if syspy is None:
            log("[ERROR] 本机找不到可用的 Python，无法创建 .venv。")
            return NO_PYTHON
        log(f"以 {syspy}（已装 {hits}/{len(DEPS)} 个依赖）为基座建 .venv，"
            f"--system-site-packages 让它已有的包直接可用 ...")
        r = run([str(syspy), "-m", "venv", "--system-site-packages",
                 str(BASE / ".venv")], timeout=600, live=True, to_stderr=True)
        if r.returncode != 0:
            return INSTALL_FAILED
        vpy = venv_python()
        if vpy is None:
            log("[ERROR] .venv 创建后仍未找到解释器。")
            return INSTALL_FAILED

    # venv 里可能没有 pip（ensurepip 被裁剪时）
    if run([str(vpy), "-m", "pip", "--version"]).returncode != 0:
        log("pip 不可用，正在引导 ...")
        r = run([str(vpy), "-m", "ensurepip", "--upgrade"])
        if r.returncode != 0:
            log(r.stdout or "", r.stderr or "")
            return INSTALL_FAILED

    req_args = ["-r", str(REQS)] if REQS.exists() else list(DEPS)
    for n, url in enumerate(PIP_INDEXES, 1):
        host = url.split("//", 1)[-1].split("/")[0]
        log(f"正在安装依赖（{host}，第 {n}/{len(PIP_INDEXES)} 个源）...")
        r = run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check",
                 "-i", url, *req_args], timeout=900, live=True, to_stderr=True)
        if r.returncode == 0 and has_deps(vpy):
            return OK
        log(f"  源 {host} 装不上，换下一个。")

    log("[ERROR] 所有 pip 源都装不上，请检查网络/代理后手动执行 0.安装依赖.bat。")
    return INSTALL_FAILED


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true", help="本机都没有依赖时自动安装")
    ap.add_argument("--print-python", action="store_true",
                    help="把应使用的解释器输出成 PY=<路径>（供 bat / 管理台取用）")
    ap.add_argument("--verbose", action="store_true", help="打印逐个探测的过程")
    args = ap.parse_args()

    py = resolve(verbose=args.verbose)
    if py is not None:
        log(f"[ok] 复用本机已有的解释器，依赖齐全，无需下载：{py}")
        if args.print_python:
            print(f"PY={py}")
        return OK

    if not args.install:
        log(f"[warn] 本机没有任何装了 {' / '.join(DEPS)} 的解释器"
            f"（便携运行时 runtime\\ 也不在）。")
        return MISSING

    log("[warn] 本机没有现成的依赖，开始安装 ...")
    rc = install()
    if rc == OK:
        py = resolve()
        if py is not None:
            log(f"[ok] 依赖已装齐：{py}")
            if args.print_python:
                print(f"PY={py}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
