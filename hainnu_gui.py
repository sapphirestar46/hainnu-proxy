"""
Hainnu Proxy · 带 GUI 的管理台

把原来拆成多个 .bat 的功能收进这一个窗口：
  * 启动 / 停止 / 重启本地桥接代理
  * 开机自启开关（等价 4.开机自启 / 5.取消自启）
  * 一键获取令牌（等价 1.获取令牌，调用 get_token.py）
  * 实时健康检查（等价 /health?probe=1）
  * 历史令牌流量统计（1小时 / 24小时 / 1周 / 1月，数据来自 usage_log.jsonl）

依赖：仅 Python 标准库 + tkinter。桥接进程用项目 .venv 的 Python 拉起，
GUI 本身用带 tkinter 的 Python（本脚本由 hainnu-gui.bat 以 py/python 启动）。

用法：
    py hainnu_gui.py        (或 python hainnu_gui.py)
"""

from __future__ import annotations

import base64
import html
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import token_codec          # token 落盘加密（Windows DPAPI）：读 token 时解密

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:  # noqa: BLE001
    sys.stderr.write(
        "[错误] 本机 Python 没有 tkinter。请用带 tkinter 的系统 Python 启动：\n"
        "       py hainnu_gui.py   或   python hainnu_gui.py\n"
        "   （项目 .venv 的 Python 可能不含 tkinter，别用它启动本程序。）\n"
    )
    sys.exit(2)

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
USAGE_LOG = BASE / "usage_log.jsonl"
SAVINGS_STATE = BASE / "savings_state.json"
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
FLASH_PRICE_FALLBACK = {
    "in_hit_off": 0.02, "in_hit_peak": 0.04,
    "in_miss_off": 1.0, "in_miss_peak": 2.0,
    "out_off": 4.0, "out_peak": 8.0,
}
# 定价页表格里我们要的那一列（用来在表头里认列；官方改名也能命中）
PRICE_MODEL_KEY = "flash"

# ---------------------------------------------------------------------------
# 价格数据源
# ---------------------------------------------------------------------------
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OR_USD_CNY = 6.667
# OpenRouter 里 flash 的模型 id 优先级（官方改名时顺着往下找）
OR_FLASH_IDS = (
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-flash-latest",
)
# 价格缓存多久算「还新鲜」（秒）。官方价格几个月才动一次，没必要每次点都爬。
PRICE_TTL = 12 * 3600
# ⚠️ 现版本：桥会把上游的 prompt_cache_hit_tokens/miss 落盘，**省钱统计与实际命中率都改用实测值**；
CACHE_HIT_RATIO_DEFAULT = 0.97

CREATE_NO_WINDOW = 0x08000000
DEVNULL = subprocess.DEVNULL

# ================== 飞书化 UI 设计规范（只管样式，不改任何功能逻辑） ==================
# 色板取自飞书设计规范：主蓝 #3370FF、浅灰底、白卡片、细边框、分层文字灰。
UI = {
    # 背景：窗口浅灰底、卡片纯白、悬停浅蓝
    "bg":              "#F5F6F7",
    "card":            "#FFFFFF",
    "hover":           "#F2F3FF",
    # 主色：飞书蓝
    "primary":         "#3370FF",
    "primary_dark":    "#2B5FD9",
    # 边框 / 分隔
    "border":          "#DEE0E3",
    "divider":         "#EFF0F1",
    # 文字分层灰
    "text_main":       "#1F2329",
    "text_regular":    "#3A3F47",
    "text_secondary":  "#646A73",
    "text_hint":       "#8F959E",
    # 语义色（对齐飞书语义色板）
    "success":         "#2EA711",
    "warning":         "#ED7B2F",
    "danger":          "#F54A45",
    "teal":            "#04B49C",   # 缓存命中（青绿）
    # 图表：输入线=主蓝；输出橙 / 命中率紫，及各自的浅色柱填充
    "chart_out":       "#FF8800",
    "chart_rate":      "#7F3BF5",
    "chart_fill_in":   "#E1EAFF",
    "chart_fill_out":  "#FFEAD1",
    "chart_fill_rate": "#ECE1FC",
}
FONT_FAMILY = "Microsoft YaHei UI"
F_TITLE   = (FONT_FAMILY, 13, "bold")   # 头部标题
F_CARD    = (FONT_FAMILY, 10, "bold")   # 卡片标题
F_BODY    = (FONT_FAMILY, 9)            # 正文
F_SMALL   = (FONT_FAMILY, 8)            # 小字说明
F_SMALL_B = (FONT_FAMILY, 8, "bold")    # 图表轴名
F_MONO_S  = ("Consolas", 8)             # URL / Key 等单宽小字


def load_port() -> int:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return int(cfg.get("port") or 8787)
    except Exception:  # noqa: BLE001
        return 8787


def load_config() -> dict:
    """读取 config.json（连接配置：上游 URL / Key / 模型 / 端口等）。"""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def runtime_binary(kind: str) -> Path | None:
    """内嵌 Python（runtime/）里的解释器：可搬迁、自带 tkinter+桥依赖，免装系统 Python。"""
    p = BASE / "runtime" / f"{kind}.exe"
    return p if p.exists() else None


def venv_binary(kind: str) -> Path | None:
    """返回 .venv 里的解释器（兜底；老部署或未带 runtime 时用）。"""
    p = BASE / ".venv" / "Scripts" / f"{kind}.exe"
    return p if p.exists() else None


def proxy_python() -> Path | None:
    return (runtime_binary("pythonw") or runtime_binary("python")
            or venv_binary("pythonw") or venv_binary("python"))


def _has_proxy_deps(py: Path) -> bool:
    """该解释器能否跑桥（需要 fastapi/uvicorn/httpx）。"""
    try:
        r = subprocess.run(
            [str(py), "-c", "import fastapi,uvicorn,httpx"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_DEPS_PY: Path | None = None
def any_python() -> Path | None:
    """随便找一个能跑脚本的解释器 —— 不要求它装了依赖。
    _deps_check.py 只用标准库，所以拿最差的系统 Python 也能把它跑起来。
    """
    cands = [Path(sys.executable)]
    for name in ("py", "python", "python3"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        try:
            if c.exists() and subprocess.run([str(c), "-c", "pass"],
                                             capture_output=True,
                                             timeout=30).returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return None
def deps_check_python() -> Path | None:
    """让 _deps_check.py 统一决定用哪个解释器。
    它会扫便携运行时 / 项目 .venv / py launcher 列出的全部版本 / conda 各 env /
    PATH 上的 python，谁已经装齐依赖就用谁 —— 本机有现成的就直接复用，
    不必再建 .venv 重下一遍。结果缓存：一次会话探一次就够。
    """
    global _DEPS_PY
    if _DEPS_PY is not None:
        return _DEPS_PY
    host = any_python()
    if host is None:
        return None
    try:
        r = subprocess.run(
            [str(host), str(BASE / "_deps_check.py"), "--print-python"],
            cwd=str(BASE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        for line in (r.stdout or "").splitlines():
            if line.startswith("PY="):
                cand = Path(line[3:].strip())
                if cand.exists():
                    _DEPS_PY = cand
                    return cand
    except Exception:  # noqa: BLE001
        pass
    return None
def select_proxy_python() -> Path | None:
    """选一个能跑桥的解释器。
    先问 _deps_check.py —— 本机某个环境里已经装过 fastapi/uvicorn/httpx 的话
    直接复用，一个包都不用下。它不可用（比如脚本缺失）时退回本地判断。
    """
    p = deps_check_python()
    if p:
        return p
    p = proxy_python()
    if p:
        return p
    cands = [Path(sys.executable)]
    for name in ("py", "python", "python3"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    seen = set()
    for cand in cands:
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if _has_proxy_deps(cand):
            return cand
    return None


def fmt_tokens(n) -> str:
    """自适应标准单位：B / M / k / 无（SI 常用单位）。配合自适应量程让 y 轴刻度一目了然。"""
    n = int(n or 0)
    if n >= 1_000_000_000:             # B (1e9)
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:                 # M (1e6)
        return f"{n / 1e6:.2f}M"
    if n >= 1000:                      # k (1e3)
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_uptime(sec: float) -> str:
    """把秒数格式化成「XdYh」/「XhYm」/「Ym」式持续运行时长。"""
    sec = int(sec or 0)
    d, r1 = divmod(sec, 86400)
    h, r2 = divmod(r1, 3600)
    m = r2 // 60                        # divmod 余数是秒，分钟要再除 60
    if d:
        return f"{d}天{h}小时{m}分"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分"


def recent_tok_rate(seconds: float = 300.0) -> float:
    """最近 N 秒的平均 tok/s（输入+输出），从 usage_log.jsonl 统计；无数据返回 0。"""
    if not USAGE_LOG.exists():
        return 0.0
    cutoff = time.time() - seconds
    acc = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if (rec.get("ts") or 0) >= cutoff:
                    acc += (rec.get("prompt") or 0) + (rec.get("completion") or 0)
    except Exception:  # noqa: BLE001
        pass
    return acc / seconds


def cumulative_tokens() -> int:
    """历史累计消耗的 tokens 总量（输入+输出，全部记录），无数据返回 0。"""
    if not USAGE_LOG.exists():
        return 0
    acc = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                acc += (rec.get("prompt") or 0) + (rec.get("completion") or 0)
    except Exception:  # noqa: BLE001
        pass
    return acc


def tokens_since(cutoff: float) -> int:
    """usage_log.jsonl 里 ts >= cutoff 的 token 总量（输入+输出）。"""
    if not USAGE_LOG.exists():
        return 0
    acc = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if (rec.get("ts") or 0) >= cutoff:
                    acc += (rec.get("prompt") or 0) + (rec.get("completion") or 0)
    except Exception:  # noqa: BLE001
        pass
    return acc


def today_tokens() -> int:
    """今日（本地时区 0 点起）消耗的 tokens 总量。

    复用 calendar_anchor(86400) 的「今日 0 点」口径，跟「24小时」那张图的起点保持一致，
    避免出现「图表口径是 0 点、数字又是滚动 24h」这种对不上的情况。
    """
    return tokens_since(calendar_anchor(86400) or 0.0)


def cache_stats_since(cutoff: float) -> dict:
    """usage_log 里 ts >= cutoff 的**缓存命中**统计。

    数据来源：桥在处理请求时把上游 usage 的 `prompt_cache_hit_tokens` /
    `prompt_cache_miss_tokens` 落成 `cache_hit` / `cache_miss` 两个字段。

    ⚠️ **加字段之前的老记录没有它** → 只计入 `unknown_requests`，不参与命中率计算；
    否则历史请求（无信息）会被当成 0 命中，把命中率拉成假的低值。
    `rate` 为 None = 还没有任何带缓存信息的记录。
    """
    st = {"hit_tokens": 0, "miss_tokens": 0, "hit_requests": 0,
          "known_requests": 0, "unknown_requests": 0, "requests": 0, "rate": None}
    if not USAGE_LOG.exists():
        return st
    try:
        with open(USAGE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if (rec.get("ts") or 0) < cutoff:
                    continue
                st["requests"] += 1
                h = rec.get("cache_hit")
                if h is None:
                    st["unknown_requests"] += 1
                    continue
                h = int(h or 0)
                st["known_requests"] += 1
                st["hit_tokens"] += h
                st["miss_tokens"] += int(rec.get("cache_miss") or 0)
                if h > 0:
                    st["hit_requests"] += 1
    except Exception:  # noqa: BLE001
        pass
    tot = st["hit_tokens"] + st["miss_tokens"]
    if tot > 0:
        st["rate"] = st["hit_tokens"] / tot * 100
    return st


def fmt_cache_line(st: dict) -> str:
    """「命中 tokens / 总 tokens = 命中率%（命中 N 次）」；无信息时如实说明。"""
    tot = st["hit_tokens"] + st["miss_tokens"]
    if not tot:
        return "—"
    return (f"{fmt_tokens(st['hit_tokens'])}/{fmt_tokens(tot)}"
            f" = {st['rate']:.1f}%（命中 {st['hit_requests']} 次）")


def nice_ceil(v: float) -> float:
    """向上取到“漂亮阶梯值”（1/1.2/1.5/2/2.5/3/4/5/6/8×10^k）。
    量程只在数据跨过上一个阶梯时才跳到下一档——日常小幅波动不改变纵轴，防蠕动。"""
    if v <= 0 or v < 1:
        return max(v, 1.0)
    mag = 10 ** int(math.floor(math.log10(v)))
    for m in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
        if m * mag >= v:
            return m * mag
    return 10 * mag


def beijing_peak(t: float | None = None) -> bool:
    """北京时间周一至周五 9:00-12:00、14:00-18:00 为高峰时段，其余为空闲时段。"""
    bj = time.gmtime((t if t is not None else time.time()) + 8 * 3600)
    return bj.tm_wday < 5 and ((9 <= bj.tm_hour < 12) or (14 <= bj.tm_hour < 18))


def time_greeting(t: float | None = None) -> str:
    """按时段返回问候语：夜深了(23-5) / 早上好(6-8) / 上午好(9-11) / 中午好(12-13) / 下午好(14-17) / 晚上好(18-22)。"""
    h = time.localtime(t).tm_hour
    if 6 <= h < 9:
        return "早上好"
    if 9 <= h < 12:
        return "上午好"
    if 12 <= h < 14:
        return "中午好"
    if 14 <= h < 18:
        return "下午好"
    if 18 <= h < 23:
        return "晚上好"
    return "夜深了"


def _load_user_name() -> str:
    """读取缓存的用户姓名（user_name.txt）；无则返回空串。"""
    try:
        return (BASE / "user_name.txt").read_text(encoding="utf-8").strip()[:32]
    except Exception:  # noqa: BLE001
        return ""


def _fetch_user_name() -> str:
    """从学校后端取当前用户的姓名（Open WebUI 的 user.name）：

    路径：读 token.txt(JWT) → 解码 payload 得用户 id → 调 GET /api/v1/users/<id>
    （该接口可穿透学校反向代理）。失败返回空串，不影响使用。
    """
    try:
        payload = (BASE / "token.txt").read_text(encoding="utf-8").strip()
        # token 落盘是 Windows DPAPI 加密（`enc:` 前缀）；旧版明文也兼容。解不出（换机/换用户）即放弃。
        token = token_codec.decrypt(payload) or ""
        up = (load_config() or {}).get("upstream") or "https://chat.hainnu.edu.cn"
    except Exception:  # noqa: BLE001
        return ""
    if not token or token.split(".").__len__() < 2:
        return ""
    try:
        b64 = token.split(".")[1].replace("-", "+").replace("_", "/")
        b64 += "=" * ((4 - len(b64) % 4) % 4)
        pid = json.loads(base64.b64decode(b64)).get("id")
    except Exception:  # noqa: BLE001
        return ""
    if not pid:
        return ""
    req = urllib.request.Request(
        f"{str(up).rstrip('/')}/api/v1/users/{pid}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return str((d or {}).get("name") or "").strip()[:32]
    except Exception:  # noqa: BLE001
        return ""


def _monotone_ys(ys, sub: int) -> list[float]:
    """对等距 x 上的 y 序列做保单调三次插值（Fritsch-Carlson 取谐均值切线）。
    结果严格经过每个原始点、逐段单调，绝不越出数据 [min, max]，从而不会下穿基线/上穿顶线。"""
    n = len(ys)
    if n < 2:
        return list(ys)
    m = [ys[i + 1] - ys[i] for i in range(n - 1)]  # 单位 x 的割线斜率(=桶值)
    ts = [0.0] * n
    ts[0] = m[0]
    ts[n - 1] = m[n - 2]
    for i in range(1, n - 1):
        if m[i - 1] * m[i] <= 0:
            ts[i] = 0.0
        else:
            ts[i] = 2.0 / (1.0 / m[i - 1] + 1.0 / m[i])  # 谐均值 ≤ 相邻割线 → 不越界
    for i in range(n - 1):
        if m[i] == 0:
            ts[i] = ts[i + 1] = 0.0
        else:
            a = ts[i] / m[i]
            b = ts[i + 1] / m[i]
            r2 = a * a + b * b
            if r2 > 9.0:
                s = 3.0 / r2 ** 0.5
                ts[i] *= s
                ts[i + 1] *= s
    out: list[float] = []
    for i in range(n - 1):
        y0, y1 = ys[i], ys[i + 1]
        t0, t1 = ts[i], ts[i + 1]
        for k in range(sub):
            u = k / sub
            u2 = u * u
            u3 = u2 * u
            h00 = 2 * u3 - 3 * u2 + 1
            h10 = u3 - 2 * u2 + u
            h01 = -2 * u3 + 3 * u2
            h11 = u3 - u2
            out.append(h00 * y0 + h10 * t0 + h01 * y1 + h11 * t1)
    out.append(ys[n - 1])
    return out


def smooth_line(pts, sub: int = 32) -> list[tuple[float, float]]:
    """把折线细分成 sub 段/节，得到高密度、保单调、不越界的平滑曲线点列。"""
    n = len(pts)
    if n < 2:
        return list(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ysd = _monotone_ys(ys, sub)
    xsd: list[float] = []
    for i in range(n - 1):
        for k in range(sub):
            xsd.append(xs[i] + (xs[i + 1] - xs[i]) * (k / sub))
    xsd.append(xs[-1])
    return list(zip(xsd, ysd))


def strip_html(s: str) -> str:
    s = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s)


def _tr_cells(row_html: str) -> list[str]:
    """把一行 <tr> 拆成单元格纯文本列表。"""
    out = []
    for c in re.findall(r"<t[dh]\b.*?</t[dh]>", row_html, flags=re.S | re.I):
        t = re.sub(r"<[^>]+>", " ", c)
        out.append(re.sub(r"\s+", " ", t).strip())
    return out


def _cell_money(cell: str) -> float | None:
    m = re.search(r"([\d.]+)\s*元", cell)
    return float(m.group(1)) if m else None


def parse_prices_from_html(raw: str, model_key: str = PRICE_MODEL_KEY) -> dict | None:
    """按 HTML 表格定位 model_key 所在列，再取该列的 6 个单价。

    官方定价页改成「多模型并排」：一个格子里挤着多个模型的价，
    纯文本正则（按数字个数取值）会直接失效。这里改成：
      1. 找表头行（首格是「模型」），数出 model_key 是第几个价格列；
      2. 逐行扫描，按「缓存命中/未命中/输出」分块 + 「空闲/高峰」取值；
      3. 每个价格行按列号取对应的那个数字。
    这样无论官方是 1 列还是 3 列、flash 在第几列，都能取对。
    """
    rows = [_tr_cells(r) for r in re.findall(r"<tr\b.*?</tr>", raw, flags=re.S | re.I)]
    rows = [c for c in rows if c]
    if not rows:
        return None

    # 1) 表头：首格「模型」，后面依次是各模型名
    col = None
    for cells in rows:
        if cells and cells[0].strip() == "模型":
            hits = [i for i, c in enumerate(cells) if i and model_key in c.lower()]
            if hits:
                col = hits[0] - 1          # 转成「价格列」里的 0-based 偏移
                break
    if col is None:
        return None

    # 2) 逐行分块取值
    found: dict[str, float] = {}
    block = None
    for cells in rows:
        joined = " ".join(cells)
        if "缓存未命中" in joined:          # 注意：必须先判「未命中」，它是「命中」的超集
            block = "in_miss"
        elif "缓存命中" in joined:
            block = "in_hit"
        elif re.search(r"tokens?\s*输出", joined):
            block = "out"
        nums = [n for n in (_cell_money(c) for c in cells) if n is not None]
        if not nums or col >= len(nums):
            continue
        if "空闲时段" in joined:
            found[block + "_off"] = nums[col]
        elif "高峰时段" in joined:
            found[block + "_peak"] = nums[col]

    need = ("in_hit_off", "in_hit_peak", "in_miss_off",
            "in_miss_peak", "out_off", "out_peak")
    if any(k not in found for k in need):
        return None
    return {k: found[k] for k in need}


def parse_flash_prices(text: str) -> dict | None:
    """纯文本兜底解析（页面不再是 <table> 时用）：每格取「第一个」价格，
    因为官方把 flash 放在第一列。"""

    def pair(seg: str) -> tuple[float, float] | None:
        off = re.search(r"空闲时段\s+([\d.]+)\s*元", seg)
        peak = re.search(r"高峰时段\s+([\d.]+)\s*元", seg, flags=re.S)
        if not off or not peak:
            return None
        return float(off.group(1)), float(peak.group(1))
    m_hit = re.search(r"（缓存命中）\s*(.*?)(?:（缓存未命中）|百万tokens)", text, flags=re.S)
    m_miss = re.search(r"（缓存未命中）\s*(.*?)百万tokens输出", text, flags=re.S)
    m_out = re.search(r"百万tokens输出\s*(.*?)(?:并发限制|扣费规则|$)", text, flags=re.S)
    if not (m_hit and m_miss and m_out):
        return None
    hit = pair(m_hit.group(1))
    miss = pair(m_miss.group(1))
    out = pair(m_out.group(1))
    if not (hit and miss and out):
        return None
    return {
        "in_hit_off": hit[0], "in_hit_peak": hit[1],
        "in_miss_off": miss[0], "in_miss_peak": miss[1],
        "out_off": out[0], "out_peak": out[1],
    }


def _get_json(url: str, timeout: int = 25, headers: dict | None = None) -> dict | list:
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_price_openrouter() -> tuple[dict | None, str]:
    """备源：OpenRouter 的 /models JSON。结构化、不用解析 HTML，但给的是美元。

    取 base pricing 当空闲价、overrides 里的最大值当高峰价，按 OR_USD_CNY 反推人民币。
    """
    try:
        data = _get_json(OPENROUTER_MODELS_URL)
    except Exception as exc:  # noqa: BLE001
        return None, f"OpenRouter 不可达：{exc}"

    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return None, "OpenRouter 返回格式异常"

    by_id = {m.get("id"): m for m in entries if isinstance(m, dict)}
    model = None
    for mid in OR_FLASH_IDS:
        if mid in by_id:
            model = by_id[mid]
            break
    if model is None:
        # 官方又改名了：退一步，找第一条「flash 且非 vision/exp/batch」的 deepseek 条目
        for m in entries:
            if not isinstance(m, dict):
                continue
            mid = (m.get("id") or "").lower()
            if ("deepseek" not in mid or "flash" not in mid
                    or any(k in mid for k in ("vision", "exp", ":batch", "0731"))):
                continue
            model = m
            break
    if model is None:
        return None, "OpenRouter 上没有找到 flash 条目"

    def usd(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    pr = model.get("pricing") or {}
    off = {"in_hit": usd(pr.get("input_cache_read")),
           "in_miss": usd(pr.get("prompt")),
           "out": usd(pr.get("completion"))}
    # 高峰价：overrides 里取各项最大值（OpenRouter 按 UTC 时段给，正好对应官方峰/谷）
    peak = dict(off)
    for ov in pr.get("overrides") or []:
        if not isinstance(ov, dict):
            continue
        for k, src in (("in_hit", "input_cache_read"), ("in_miss", "prompt"), ("out", "completion")):
            v = usd(ov.get(src))
            if v is not None and (peak[k] is None or v > peak[k]):
                peak[k] = v
    if any(v is None for v in (*off.values(), *peak.values())):
        return None, "OpenRouter 价格字段缺失"

    # OpenRouter 的单位是「USD / 每 token」，先 ×1e6 换成「USD / 百万 token」，再折人民币
    cny = OR_USD_CNY * 1e6
    p = {
        "in_hit_off": off["in_hit"] * cny, "in_hit_peak": peak["in_hit"] * cny,
        "in_miss_off": off["in_miss"] * cny, "in_miss_peak": peak["in_miss"] * cny,
        "out_off": off["out"] * cny, "out_peak": peak["out"] * cny,
    }
    # sanity check：换算关系若变了（或匹配到了错误的模型），数字会明显跑偏，宁可不用
    for k, v in p.items():
        base = FLASH_PRICE_FALLBACK[k]
        if not (0.2 * base <= v <= 5.0 * base):
            return None, f"OpenRouter 换算结果异常（{k}={v:g}），已忽略"
    return {k: round(v, 6) for k, v in p.items()}, ""


def fetch_flash_prices() -> tuple[dict | None, str, str]:
    """按「官方 HTML → OpenRouter JSON」的顺序取价。

    返回 (价格, 错误信息, 来源)。来源用于界面标注，让她知道数字是哪来的。
    """
    # 1) 官方 HTML：权威、人民币
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        err_official = f"官方页不可达：{exc}"
        raw = ""
    else:
        p = parse_prices_from_html(raw) or parse_flash_prices(strip_html(raw))
        if p is not None:
            return p, "", "官方页"
        err_official = "官方页结构变化，解析失败"

    # 2) OpenRouter：结构化兜底
    p, err_or = fetch_price_openrouter()
    if p is not None:
        return p, "", "OpenRouter"
    return None, f"{err_official}；{err_or}", ""


def _load_savings_state() -> dict:
    base = {"saved_yuan": 0.0, "last_ts": 0.0, "price": None,
            "price_fetched_at": 0.0, "price_source": ""}
    try:
        if SAVINGS_STATE.exists():
            d = json.loads(SAVINGS_STATE.read_text(encoding="utf-8"))
            base.update({k: d[k] for k in base if k in d})
    except Exception:  # noqa: BLE001
        pass
    return base

_PILLOW_OK = None


def ensure_pillow() -> bool:
    """确保 Pillow 可用（抗锯齿渲染用）。缺则按需安装一次；返回是否可用。"""
    global _PILLOW_OK
    if _PILLOW_OK is not None:
        return _PILLOW_OK
    try:
        import PIL  # noqa: F401
        _PILLOW_OK = True
    except Exception:  # noqa: BLE001
        try:
            py = Path(sys.executable)
            subprocess.run(
                [str(py), "-m", "pip", "install", "-q",
                 "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "Pillow"],
                capture_output=True, timeout=120)
            import PIL  # noqa: F401
            _PILLOW_OK = True
        except Exception:  # noqa: BLE001
            _PILLOW_OK = False
    return _PILLOW_OK


def calendar_anchor(sec: int) -> float | None:
    """返回本地墙钟“起点”的 UTC epoch：24h→今日0点；周→本周一0点；月→本月1号0点；1h→None(滑动)。"""
    if sec <= 3600:
        return None
    lt = time.localtime()
    if sec == 86400:                      # 24h：今日 0 点
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, -1, -1, -1))
    if sec == 604800:                     # 周：本周一 0 点（tm_wday=0 即周一）
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday,
                            0, 0, 0, -1, -1, -1))
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, -1, -1, -1))  # 月：1号0点

# 时间窗: (按钮名, 秒, 柱数)
WINDOWS = [
    ("1小时", 3600, 60),        # 1分钟/桶
    ("24小时", 86400, 144),     # 10分钟/桶
    ("1周", 604800, 168),       # 1小时/桶（7天×24；每天1个端点太少）
    ("1月", 2_592_000, 60),     # 半天(12h)/桶（30天×2）
]
TABLE_PAGE = 50                  # 用量表每页条数


def agg_usage(window: int, nbuckets: int):
    """读取 usage_log.jsonl，按输入(prompt)/输出(completion)分开统计每个桶的 tokens。

    返回 `(bins, bout, total_in, total_out, count, brate, (total_hit, total_miss))`；
    其中 `brate[i]` 是第 i 桶的**缓存命中率(%)**，该桶没有带缓存信息的记录时为 None
    （曲线在该处断开，而不是画成 0 —— 老记录没有 cache_hit 字段）。

    分桶锚定到墙钟绝对时段（本地整分/整时/零点），而非相对\"当前时刻\"滑动：
    历史记录的桶归属只在整时段跳变时才整体前移，刷新间隔内保持固定，曲线不再蠕动；
    纵轴标度也因窗口内容稳定而自动稳定，只有新 tokens 在最新(最右)桶内增长。
    """
    step = int(window / nbuckets)          # step 均整秒：60(1h)/600(24h)/86400(周·月)
    now = time.time()
    tzoff = time.localtime().tm_gmtoff     # 本地时区距 UTC 秒数，对齐到本地时段边界
    # 统一“墙钟锚定到固定槽位”：1h 锚到最近 N 个整分钟槽（每分钟整块平移，不逐条爬动）；
    # 24h/周/月锚到日历边界(今日0点/周一/1号)。历史桶归属在槽内固定，不随 now 迁移。
    ks = calendar_anchor(window)
    k_n = int((now + tzoff) // step)       # 当前时刻所在绝对槽
    if ks is not None:
        k_start = int((ks + tzoff) // step)
    else:
        k_start = k_n - (nbuckets - 1)     # 1h：窗口左端=当前分钟槽前 N-1 个整分钟槽
    bins = [0] * nbuckets   # 每桶输入 tokens
    bout = [0] * nbuckets   # 每桶输出 tokens
    bhit = [0] * nbuckets   # 每桶缓存命中 tokens（prompt）
    bmiss = [0] * nbuckets  # 每桶缓存未命中 tokens（prompt）
    count = 0
    total_in = 0
    total_out = 0
    total_hit = 0
    total_miss = 0
    if USAGE_LOG.exists():
        try:
            with open(USAGE_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    ts = rec.get("ts", 0)
                    pin = rec.get("prompt", 0) or 0
                    pout = rec.get("completion", 0) or 0
                    k = int((ts + tzoff) // step)          # 该条所属的绝对槽
                    idx = k - k_start                      # 自窗口起点起的索引
                    if 0 <= idx < nbuckets:                # 落在本窗口内
                        count += 1
                        total_in += pin
                        total_out += pout
                        bins[idx] += pin
                        bout[idx] += pout
                        # 缓存命中/未命中（只有桥落过字段的记录才有；老记录跳过）
                        ch = rec.get("cache_hit")
                        if ch is not None:
                            bhit[idx] += int(ch or 0)
                            bmiss[idx] += int(rec.get("cache_miss") or 0)
                            total_hit += int(ch or 0)
                            total_miss += int(rec.get("cache_miss") or 0)
        except Exception:  # noqa: BLE001
            pass
    # 每桶的缓存命中率（%）；该桶没有带缓存信息的记录 → None（曲线断开，不画 0）
    brate = [None] * nbuckets
    for i in range(nbuckets):
        tot = bhit[i] + bmiss[i]
        if tot > 0:
            brate[i] = bhit[i] / tot * 100.0
    return bins, bout, total_in, total_out, count, brate, (total_hit, total_miss)

PRUNE_INTERVAL = 60.0          # 多久做一次缓存裁剪（秒）
_MAX_WINDOW = max(sec for _, sec, _ in WINDOWS)   # 最长时间窗（1月）


def prune_usage_log() -> None:
    """删掉超出时间窗的历史记录（缓存裁剪）：
    只保留最近 [_MAX_WINDOW + 1天] 的记录——任何可选时间窗都够用，旧数据及时丢弃、文件不膨胀。
    已计入省钱累计的旧行（ts 已 < last_ts）不会影响后续累计。"""
    if not USAGE_LOG.exists():
        return
    cutoff = time.time() - (_MAX_WINDOW + 86400)
    try:
        lines = USAGE_LOG.read_text(encoding="utf-8").splitlines()
        keep = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:  # noqa: BLE001
                keep.append(ln)   # 解析失败的行保留（不误删）
                continue
            if (rec.get("ts") or 0) >= cutoff:
                keep.append(ln)
        if len(keep) < len(lines):
            USAGE_LOG.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def listener_pids(port: int) -> set[int]:
    """谁在监听这个端口。

    只认 TCP 的 LISTENING 行，并且用「本地地址」那一列精确比端口 ——
    老写法是「整行含 :8787 且含 LISTENING」，`:87870` 这类会误命中，
    「本机连到 8787 的客户端行」也可能被算进来。
    """
    pids: set[int] = set()
    try:
        out = subprocess.run("netstat -ano", capture_output=True, text=True,
                             errors="replace").stdout or ""
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


def kill_pid(pid: int) -> bool:
    try:
        r = subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True, text=True, errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def stop_proxy_processes(port: int | None = None) -> int:
    """停止桥进程，返回杀掉的个数。

    ⚠️ **默认只按端口精确杀，不再按命令行全杀。**
    老实现是「杀掉所有命令行含 hainnu_proxy 的 python」——只要机器上还有
    别人/别的实例在跑同一个桥，就会被一起干掉（踩过一次，被用户提醒
    「有别人在同时测试，别误杀」）。
    传 port=None 才退化成全杀（等价 6.关闭代理.bat 的「全部清掉」语义）。
    """
    if port:
        pids = listener_pids(port)
        return sum(1 for pid in pids if kill_pid(pid))
    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name like '%python%'\" | "
        "Where-Object { $_.CommandLine -like '*hainnu_proxy*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    except Exception:  # noqa: BLE001
        pass
    return -1                       # 全杀模式没法可靠计数


class HainnuGUI(tk.Tk):

    def __init__(self):
        super().__init__()
        self.geometry("1240x860")
        self.minsize(1040, 720)
        self.configure(bg=UI["bg"])
        self._user_name = _load_user_name()   # 登录时保存的姓名（get_token.py 写入 user_name.txt）
        self._last_greet_hour = time.localtime().tm_hour
        _gs = self._greet_suffix()
        if _gs:
            self.title("Hainnu Proxy · 管理台    |    " + _gs)
        else:
            self.title("Hainnu Proxy · 管理台")

        self.port = load_port()
        # 常规定位只探“本地桥是否存活”（不 probe 上游），快且不与学校后端状态耦合，
        # 避免“学校上游一抽风/限流，本地代理就被误判成离线”。
        self.health_url = f"http://127.0.0.1:{self.port}/health"
        # 手动“健康自检”才带 ?probe=1（真正打一次学校上游）
        self.health_url_probe = f"http://127.0.0.1:{self.port}/health?probe=1"
        self.q: queue.Queue = queue.Queue()
        self._offline_streak = 0   # 连续探测失败计数：≥2 次才判离线（去抖，防偶发抖动）
        self._backend_state = None  # 最近一次上游 autoprobe 得出的后端状态(text,fg)，常规轮询不覆盖
        self._probe_n = 0           # 常规轮询计数，每 PROBE_EVERY 次补一次上游自检
        self._stop = False
        self._window = (WINDOWS[1][1], WINDOWS[1][2])  # 默认 24小时 → (秒, 柱数)

        # 官方价格 + 省钱累计（跨重启持久化）
        st = _load_savings_state()
        self.saved_yuan = st["saved_yuan"]
        self.last_ts = st["last_ts"]
        self.price_fetched_at = st["price_fetched_at"]
        # 老版本存档没有 price_source 字段：有价但不知来源就标「历史缓存」
        self.price_source = st["price_source"] or ("内置基准" if not st["price"] else "历史缓存")
        self.price = st["price"] or dict(FLASH_PRICE_FALLBACK)
        self._price_fetching = False   # 避免并发重复拉价
        self._last_prune = 0.0
        self._rl_state = "ok"      # 限流提醒状态: ok/recent/now（用于状态条高亮提示）
        self._rl_restart_at = 0.0  # 限流冷却结束后 + 保守缓冲，届时自动重启代理的时间戳
        self._rl_last_auto = 0.0   # 上次自动重启时刻（防止刚重启又撞限流、再重启）
        self._rl_auto_hour = 0     # 计数所属小时（for 本小时自动重启次数上限）
        self._rl_auto_cnt = 0      # 本小时已自动重启次数（保守上限 3）
        self._proxy_proc = None   # 我们自己 Popen 出来的桥子进程（精确回收用）
        self._proxy_log = None    # 该子进程的输出文件句柄
        self._chart_hover_i = None  # 图表悬停所在桶索引（None=未悬停）
        self._chart_hover_x = 0     # 悬停时的鼠标 X，用于浮窗定位
        self._online_since = None   # 观察到的“代理持续在线”起点（桥未上报 uptime 时兜底算时长）

        self._build_ui()
        self._update_peak_state()
        # 后台健康探测线程
        threading.Thread(target=self._probe_loop, daemon=True).start()
        # 后台取姓名（读 token.txt + 学校后端 /api/v1/users/<id>），完成后刷新问候
        threading.Thread(target=self._load_name_async, daemon=True).start()
        # 启动时读一次真实开机自启状态，让按钮准确显示「已设开机自启 / 启用开机自启」
        threading.Thread(target=self._init_autostart_label, daemon=True).start()
        # 价格：先用缓存/基准价把标签填好（否则一直显示“未获取”），
        # 缓存过期才后台静默拉一次 —— 官方价格几个月才动一次，不必每次启动都爬。
        self._refresh_price_label()
        self._maybe_auto_update_price()
        self.after(300, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI 构建

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # ---- 纯样式小工具：卡片 / 按钮 / 标签 / 输入框（不改变任何交互行为） ----
        def card(parent, title):
            """飞书式白卡片：白底 + 1px 细边框 + 卡片标题行 + 标题下细分隔线。"""
            c = tk.Frame(parent, bg=UI["card"], bd=0,
                         highlightbackground=UI["border"], highlightthickness=1)
            tk.Label(c, text=title, bg=UI["card"], fg=UI["text_main"],
                     font=F_CARD, anchor="w").pack(fill="x", padx=12, pady=(10, 4))
            tk.Frame(c, bg=UI["divider"], height=1, bd=0,
                     highlightthickness=0).pack(fill="x", padx=12)
            body = tk.Frame(c, bg=UI["card"])
            body.pack(fill="both", expand=True, padx=12, pady=(8, 10))
            return c, body

        def btn(parent, text, cmd, width, kind="secondary", font=F_BODY):
            """飞书两级按钮：primary=蓝底白字，secondary=白底描边；悬停仅变底色。"""
            if kind == "primary":
                bg, fg = UI["primary"], "#FFFFFF"
                abg, afg = UI["primary_dark"], "#FFFFFF"
                edge = UI["primary"]
            else:
                bg, fg = UI["card"], UI["text_regular"]
                abg, afg = UI["hover"], UI["text_main"]
                edge = UI["border"]
            b = tk.Button(parent, text=text, width=width, command=cmd,
                          bg=bg, fg=fg, activebackground=abg, activeforeground=afg,
                          relief="flat", bd=0, highlightthickness=1,
                          highlightbackground=edge, highlightcolor=edge,
                          font=font, cursor="hand2", takefocus=0)
            b.bind("<Enter>", lambda _e, w=b, c=abg: w.config(bg=c))
            b.bind("<Leave>", lambda _e, w=b, c=bg: w.config(bg=c))
            return b

        def lbl(parent, text, fg=None, font=F_BODY, **kw):
            """卡片内标签：默认白底、正文灰。"""
            return tk.Label(parent, text=text, bg=UI["card"],
                            fg=fg or UI["text_regular"], font=font, **kw)

        def entry(parent, var):
            """飞书式输入框：白底细边框，聚焦变主蓝描边。"""
            return tk.Entry(parent, textvariable=var, font=F_MONO_S,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightbackground=UI["border"],
                            highlightcolor=UI["primary"],
                            bg=UI["card"], fg=UI["text_main"],
                            insertbackground=UI["text_main"])

        # ttk（仅「模型」下拉一个）统一到飞书观感：白底、细边、聚焦主蓝
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure("Feishu.TCombobox",
                        fieldbackground=UI["card"], background=UI["card"],
                        foreground=UI["text_main"], bordercolor=UI["border"],
                        lightcolor=UI["card"], darkcolor=UI["card"],
                        arrowcolor=UI["text_secondary"], padding=2)
        style.map("Feishu.TCombobox",
                  bordercolor=[("active", UI["primary"]), ("focus", UI["primary"])],
                  fieldbackground=[("readonly", UI["card"])],
                  foreground=[("readonly", UI["text_main"])])
        style.configure("Usage.Treeview",
                        background=UI["card"], fieldbackground=UI["card"],
                        foreground=UI["text_main"], bordercolor=UI["border"],
                        lightcolor=UI["card"], darkcolor=UI["card"],
                        rowheight=28, font=F_BODY)
        style.configure("Usage.Treeview.Heading",
                        background=UI["card"], foreground=UI["text_secondary"],
                        font=F_SMALL_B, relief="flat", padding=(6, 4))
        style.map("Usage.Treeview",
                  background=[("selected", UI["hover"])],
                  foreground=[("selected", UI["text_main"])])
        style.configure("Usage.Treeview", indent=0)

        # 头部：白底标题栏 + 状态点，下方一条细分隔线
        head = tk.Frame(self, bg=UI["card"])
        head.pack(fill="x")
        self.l_head = tk.Label(head, text="Hainnu Proxy · 本地 OpenAI / Anthropic 接口",
                               bg=UI["card"], fg=UI["text_main"], font=F_TITLE)
        self.l_head.pack(side="left", padx=20, pady=14)
        self.status_dot = tk.Canvas(head, width=16, height=16, bg=UI["card"],
                                    highlightthickness=0)
        self.status_dot.pack(side="right", padx=(0, 8))
        self.status_txt = tk.Label(head, text="未检测", bg=UI["card"],
                                   fg=UI["text_hint"], font=F_BODY)
        self.status_txt.pack(side="right", padx=(0, 10))
        tk.Frame(self, bg=UI["divider"], height=1, bd=0,
                 highlightthickness=0).pack(fill="x")

        # 顶部三栏横排（横向紧凑放下）：运行状态 / 费用估算·省钱 / 连接配置
        top_row = tk.Frame(self, bg=UI["bg"])
        top_row.pack(fill="x", **pad)

        box, box_body = card(top_row, "运行状态")
        box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.l_run = lbl(box_body, "代理：—", fg=UI["text_main"], anchor="w")
        self.l_run.pack(fill="x")
        # 持续运行时长、速率各自独立一行（不挤进“代理”那一行小字）
        self.l_uptime = lbl(box_body, "持续运行：—", anchor="w")
        self.l_uptime.pack(fill="x", pady=(6, 0))
        self.l_speed = lbl(box_body, "速率：—", anchor="w")
        self.l_speed.pack(fill="x")
        self.l_speed_live = lbl(box_body, "实时输出：—", fg=UI["primary"], anchor="w")
        self.l_speed_live.pack(fill="x")
        self.l_health = lbl(box_body, "后端：—", fg=UI["text_hint"], anchor="w")
        self.l_health.pack(fill="x", pady=(6, 0))
        self.l_models = lbl(box_body, "模型：—", fg=UI["text_hint"], anchor="w")
        self.l_models.pack(fill="x")
        # 限流警告：实时反映桥接的 429 自动冷却 / 指数退避重试保护状态
        self.l_rl = lbl(box_body, "限流：—", fg=UI["text_hint"], anchor="w")
        self.l_rl.pack(fill="x")

        cost, cost_body = card(top_row, "费用估算 · 省钱")
        cost.pack(side="left", fill="both", expand=True, padx=8)
        prow = tk.Frame(cost_body, bg=UI["card"])
        prow.pack(fill="x")
        self.btn_price = btn(prow, "更新价格", self._fetch_price_now, 8)

        # ---- 统计瓦片（视觉参考 sub2api 仪表盘）：浅色圆角芯片 + 大数字 + 小字明细 ----
        TILE_BG = "#F7F8FA"                       # 瓦片底：比卡片白底略深，撑出「卡中卡」层次

        def stat_tile(parent, chip_bg, chip_fg, glyph, caption, value_fg):
            t = tk.Frame(parent, bg=TILE_BG, highlightbackground=UI["divider"],
                         highlightthickness=1)
            head = tk.Frame(t, bg=TILE_BG)
            head.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(head, text=glyph, bg=chip_bg, fg=chip_fg,
                     font=(FONT_FAMILY, 11, "bold"), width=2).pack(side="left")
            col = tk.Frame(head, bg=TILE_BG)
            col.pack(side="left", fill="x", expand=True, padx=(8, 0))
            # 瓦片内部底色是 TILE_BG 而非卡片白，故不走 lbl()（它固定 bg=card）
            tk.Label(col, text=caption, bg=TILE_BG, fg=UI["text_secondary"],
                     font=F_SMALL, anchor="w").pack(fill="x")
            v = tk.Label(col, text="—", bg=TILE_BG, fg=value_fg,
                         font=(FONT_FAMILY, 13, "bold"), anchor="w")
            v.pack(fill="x")
            sub = tk.Label(t, text=" ", bg=TILE_BG, fg=UI["text_hint"],
                           font=F_SMALL, anchor="w", justify="left", wraplength=150)
            sub.pack(fill="x", padx=8, pady=(1, 6))
            return t, v, sub

        grid = tk.Frame(cost_body, bg=UI["card"])
        grid.pack(fill="x", pady=(6, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="stat")

        t1, self.l_saved, self.l_saved_sub = stat_tile(
            grid, "#D1FAE5", "#059669", "¥", "累计节省", UI["success"])
        t1.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        t2, self.l_tokens_today, self.l_tokens_today_sub = stat_tile(
            grid, "#DBEAFE", "#2563EB", "今", "今日消耗", UI["text_main"])
        t2.grid(row=0, column=1, sticky="nsew", pady=(0, 6))
        t3, self.l_tokens_total, self.l_tokens_total_sub = stat_tile(
            grid, "#E0E7FF", "#4F46E5", "Σ", "累计消耗", UI["text_main"])
        t3.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        t4, self.l_cache, self.l_cache_sub = stat_tile(
            grid, "#EDE9FE", "#7C3AED", "%", "缓存命中（今日）", UI["text_main"])
        t4.grid(row=1, column=1, sticky="nsew")

        # 官方峰/谷状态：峰值=波峰(红加粗“梁文峰”)，空闲=波谷(绿加粗“梁文谷”)
        pkrow = tk.Frame(cost_body, bg=UI["card"])
        pkrow.pack(fill="x", pady=(8, 0))
        lbl(pkrow, "官方当前是：", fg=UI["text_secondary"], anchor="w").pack(side="left")
        self.l_peak_state = tk.Label(pkrow, text="…", bg=UI["card"],
                                     font=(FONT_FAMILY, 10, "bold"))
        self.l_peak_state.pack(side="left")
        self.l_price = lbl(cost_body, "官方价：未获取", anchor="w", font=F_SMALL,
                           fg=UI["text_secondary"])
        self.l_price.pack(fill="x", pady=(4, 0))
        # 空闲/高峰时段 + 是否已联网更新：独立一行（原与价格挤一行、太长）
        self.l_price_note = lbl(cost_body, "空闲时段 …", anchor="w", font=F_SMALL,
                                fg=UI["text_secondary"])
        self.l_price_note.pack(fill="x")

        cfg, cfg_body = card(top_row, "连接配置")
        cfg.pack(side="left", fill="both", expand=True, padx=(8, 0))
        cdata = load_config()

        def cfg_row(label, var, readonly=False):
            r = tk.Frame(cfg_body, bg=UI["card"])
            r.pack(fill="x", pady=2)
            # 宽度给足：中文是双宽字符，width=5 会裁掉「本地ANTH」这类较长前缀（被 URL 前的文字遮挡）
            lbl(r, label, width=11, anchor="w", font=F_SMALL,
                fg=UI["text_secondary"]).pack(side="left")
            e = entry(r, var)
            if readonly:
                # 只读展示（值由程序推导，不该手改）；仍可全选复制
                e.config(state="readonly")
            e.pack(side="left", fill="x", expand=True, ipady=2)
            btn(r, "复制", lambda v=var: (
                self.clipboard_clear(), self.clipboard_append(v.get()), self.update()
            ), 4, font=F_SMALL).pack(side="left", padx=(6, 0))

        self._c_key = tk.StringVar(value=str(cdata.get("local_api_key", "")))
        # 模型：直接放**上游抓到的真实模型名**（_set_model_choices 每轮健康检查时校正）。
        # 初值取 config 里配的；若过期/为空，第一次拉到上游列表就会被换成真名。
        self._c_mod = tk.StringVar(value=str(cdata.get("default_model") or ""))
        self._c_port = tk.StringVar(value=str(cdata.get("port", "")))
        _lport = int(self._c_port.get() or 8787)
        # 两个本地代理 URL 放进「可改 + 可复制」的文字栏（不显示上游学校 URL）
        self._c_url_oai = tk.StringVar(value=f"http://127.0.0.1:{_lport}/v1")
        self._c_url_an = tk.StringVar(value=f"http://127.0.0.1:{_lport}/v1/messages")
        cfg_row("本地OAI", self._c_url_oai)
        cfg_row("本地ANTH", self._c_url_an)
        cfg_row("Key", self._c_key)

        mrow = tk.Frame(cfg_body, bg=UI["card"])
        mrow.pack(fill="x", pady=2)
        lbl(mrow, "模型", width=11, anchor="w", font=F_SMALL,
            fg=UI["text_secondary"]).pack(side="left")
        mhold = tk.Frame(mrow, bg=UI["card"])
        mhold.pack(side="left", fill="x", expand=True)
        self._c_mod_user_edited = False      # True = 用户手改过，别再自动填
        self._c_mod_entry = entry(mhold, self._c_mod)
        self._c_mod_box = ttk.Combobox(mhold, textvariable=self._c_mod,
                                       font=F_MONO_S, values=[],
                                       style="Feishu.TCombobox")
        for _w in (self._c_mod_entry, self._c_mod_box):
            _w.bind("<KeyRelease>", self._on_mod_edited)
        self._c_mod_box.bind("<<ComboboxSelected>>", self._on_mod_edited)
        self._c_mod_entry.pack(fill="x", ipady=2)     # 默认先按单模型显示
        # 手动刷新：直接问桥要一次 /v1/models（桥会强制刷新自己的缓存并回上游真名）
        btn(mrow, "刷新", self._reload_models, 4, font=F_SMALL).pack(
            side="left", padx=(6, 0))

        cfg_row("端口", self._c_port)

        # ---- 直连设置（只读展示 + 一键复制）：让支持自定义供应商的客户端跳过
        # 本地桥、直连学校上游。URL 由 config.json 的 upstream 推导；Key 与
        # token.txt 同源（DPAPI 解密，等同密码），随 GUI 启动读取一次。
        _up = str(cdata.get("upstream") or "").rstrip("/")
        self._c_url_direct = tk.StringVar(
            value=(_up + "/api") if _up else "（config.json 缺 upstream，无法直连）")
        cfg_row("直连URL", self._c_url_direct, readonly=True)
        try:
            _jwt_direct = token_codec.decrypt(
                (BASE / "token.txt").read_text(encoding="utf-8").strip()) or ""
        except Exception:  # noqa: BLE001
            _jwt_direct = ""
        self._c_key_direct = tk.StringVar(
            value=_jwt_direct or "未获取（先双击「1.获取令牌.bat」）")
        cfg_row("直连Key", self._c_key_direct, readonly=True)

        self._cfg_status = lbl(cfg_body, "改后点「保存」，重启代理生效。",
                               anchor="w", font=F_SMALL, fg=UI["text_hint"])
        self._cfg_status.pack(fill="x", pady=(4, 0))
        # 模型那一行的说明（数量/来源/是否与上游脱节）
        self._cfg_model_note = lbl(cfg_body, "模型来自学校上游，正在读取…",
                                   anchor="w", font=F_SMALL, fg=UI["text_hint"],
                                   wraplength=250, justify="left")
        self._cfg_model_note.pack(fill="x")
        btn(cfg_body, "保存", self._save_config_inline, 10, kind="primary",
            font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", pady=(8, 0))

        # 控制按钮
        ctrl = tk.Frame(self, bg=UI["bg"])
        ctrl.pack(fill="x", **pad)
        self.btn_start = btn(ctrl, "▶ 启动代理", self._start_proxy, 12,
                             kind="primary", font=(FONT_FAMILY, 9, "bold"))
        self.btn_start.pack(side="left")
        self.btn_stop = btn(ctrl, "■ 停止代理", self._stop_proxy, 12)
        self.btn_stop.pack(side="left", padx=6)
        self.btn_restart = btn(ctrl, "↻ 重启代理", self._restart_proxy, 12)
        self.btn_restart.pack(side="left")
        self.btn_auto = btn(ctrl, "启用开机自启", self._toggle_autostart, 14)
        self.btn_auto.pack(side="left", padx=(12, 0))
        # 令牌按钮区（右侧一组）：灰字提示 | 获取令牌 | 备用登录
        tframe = tk.Frame(ctrl, bg=UI["bg"])
        tframe.pack(side="right")
        self.l_token_hint = tk.Label(tframe, anchor="e", justify="right",
                                     fg=UI["text_hint"], bg=UI["bg"], font=F_SMALL,
                                     text="请不要将cookie或您的程序发给别人，本平台不会上传您的信息。")
        self.l_token_hint.pack(side="left", padx=(0, 8))
        self.btn_token = btn(tframe, "获取令牌(浏览器登录)", self._get_token, 18,
                             kind="primary", font=(FONT_FAMILY, 9, "bold"))
        self.btn_token.pack(side="left")
        self.btn_legacy = btn(tframe, "备用登录", self._get_token_legacy, 10)
        self.btn_legacy.pack(side="left", padx=6)

        # 底部一行：自检 / 刷新
        foot = tk.Frame(self, bg=UI["bg"])
        foot.pack(fill="x", **pad)
        self.btn_check = btn(foot, "健康自检", self._health_now, 12)
        self.btn_check.pack(side="left")
        self.btn_refresh = btn(foot, "刷新", self._health_now, 8)
        self.btn_refresh.pack(side="left", padx=6)
        self.txt = tk.Label(foot, text="", fg=UI["success"], bg=UI["bg"],
                            anchor="w", font=F_BODY)
        self.txt.pack(side="left", fill="x", expand=True, padx=10)

        # 用量面板：标题行右侧「曲线 / 表格」切换（表格只展示 usage_log 里实际有的字段）
        use = tk.Frame(self, bg=UI["card"], bd=0,
                       highlightbackground=UI["border"], highlightthickness=1)
        use.pack(fill="both", expand=True, **pad)
        title_row = tk.Frame(use, bg=UI["card"])
        title_row.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(title_row, text="历史令牌流量（tokens）", bg=UI["card"],
                 fg=UI["text_main"], font=F_CARD, anchor="w").pack(side="left")
        sw = tk.Frame(title_row, bg=UI["card"])
        sw.pack(side="right")
        self.var_view = tk.StringVar(value="chart")
        self.l_view_chart = tk.Label(sw, text="曲线", bg=UI["card"],
                                     fg=UI["text_main"], font=F_SMALL)
        self.l_view_chart.pack(side="left")
        self._sw = tk.Canvas(sw, width=40, height=22, bg=UI["card"],
                             highlightthickness=0, cursor="hand2")
        self._sw.pack(side="left", padx=6)
        self._sw.bind("<Button-1>", lambda _e: self._set_usage_view(
            "table" if self.var_view.get() == "chart" else "chart"))
        self.l_view_table = tk.Label(sw, text="表格", bg=UI["card"],
                                     fg=UI["text_hint"], font=F_SMALL)
        self.l_view_table.pack(side="left")
        self._paint_switch()
        tk.Frame(use, bg=UI["divider"], height=1, bd=0,
                 highlightthickness=0).pack(fill="x", padx=12)
        use_body = tk.Frame(use, bg=UI["card"])
        use_body.pack(fill="both", expand=True, padx=12, pady=(8, 10))

        row = tk.Frame(use_body, bg=UI["card"])
        row.pack(fill="x")
        self.var_win = tk.StringVar(value="24小时")
        for name, _sec, _nb in WINDOWS:
            tk.Radiobutton(row, text=name, variable=self.var_win, value=name,
                           command=self._on_window, font=F_BODY,
                           bg=UI["card"], fg=UI["text_regular"],
                           activebackground=UI["card"], activeforeground=UI["text_main"],
                           selectcolor="#FFFFFF", highlightthickness=0,
                           cursor="hand2").pack(side="left")
        self.l_sum = tk.Label(row, text="总计 — · 请求 —", font=F_BODY,
                              bg=UI["card"], fg=UI["text_secondary"])
        self.l_sum.pack(side="right")

        self.canvas = tk.Canvas(use_body, bg=UI["card"], height=240,
                                highlightthickness=1,
                                highlightbackground=UI["border"])
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))

        self.tbl_frame = tk.Frame(use_body, bg=UI["card"])
        tree_row = tk.Frame(self.tbl_frame, bg=UI["card"])
        tree_row.pack(fill="both", expand=True)
        cols = ("ts", "effort", "tokens", "ms", "cost", "rate")
        self.usage_tree = ttk.Treeview(tree_row, columns=cols,
                                       show="tree headings", style="Usage.Treeview")
        self.usage_tree.heading("#0", text="模型")
        self.usage_tree.column("#0", width=200, minwidth=90, stretch=False, anchor="w")
        heads = (("ts", "时间", 120, "w", False),
                 ("effort", "推理等级", 84, "w", False),
                 ("tokens", "输入 / 缓存 / 输出", 300, "e", False),
                 ("ms", "用时", 72, "e", False),
                 ("cost", "花费", 96, "e", False),
                 ("rate", "命中率", 84, "e", False))
        for cid, text, w, anc, stretch in heads:
            self.usage_tree.heading(cid, text=text)
            self.usage_tree.column(cid, width=w, minwidth=48, anchor=anc, stretch=stretch)
        self._ico_ds = self._load_ds_icon()
        sb = ttk.Scrollbar(tree_row, orient="vertical",
                           command=self.usage_tree.yview)
        self.usage_tree.configure(yscrollcommand=sb.set)
        self.usage_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pager = tk.Frame(self.tbl_frame, bg=UI["card"])
        pager.pack(fill="x", pady=(6, 0))
        self.btn_tbl_prev = btn(pager, "上一页", lambda: self._tbl_goto(self._tbl_page - 1),
                                8, font=F_SMALL)
        self.btn_tbl_prev.pack(side="left")
        self.l_tbl_page = tk.Label(pager, text="第 1 / 1 页", bg=UI["card"],
                                   fg=UI["text_secondary"], font=F_SMALL)
        self.l_tbl_page.pack(side="left", padx=10)
        self.btn_tbl_next = btn(pager, "下一页", lambda: self._tbl_goto(self._tbl_page + 1),
                                8, font=F_SMALL)
        self.btn_tbl_next.pack(side="left")
        self.l_tbl_count = tk.Label(pager, text="", bg=UI["card"],
                                    fg=UI["text_hint"], font=F_SMALL)
        self.l_tbl_count.pack(side="right")
        self._tbl_stamp = None
        self._tbl_recs: list = []
        self._tbl_page = 0

        self._draw_empty()

    # ---------------------------------------------------------------- 模型栏

    def _on_mod_edited(self, event=None):
        """用户动了「模型」栏 —— 记为「手动指定」，之后不再被上游真名自动覆盖。

        上游真名只是**默认值**；一旦用户自己填了，就以用户的为准。
        （把这一栏**清空**即可回到上游真名 —— 下一轮会自动填上。）
        """
        self._c_mod_user_edited = True

    def _set_model_choices(self, ids) -> None:
        """用上游模型列表刷新「模型」那一栏。

        规则（按用户要求）：
          · **直接显示上游抓到的真实模型名**，不用"跟随上游"之类的占位文字；
          · 上游只有 1 个 → 普通输入框，预填它的真名；
          · 上游多于 1 个 → 可编辑下拉（下拉选或自己手打都行），选项=上游真名列表；
          · **可修改**：上游真名只是"默认值"。用户手改过之后（_c_mod_user_edited）
            我们就不再自动覆盖，只更新下拉选项与下面的说明。

        「自动填」的三种情形：值为空 → 填上游第一个；我们填过但该名字已不在上游
        （学校换 id）→ 跟着换成上游第一个；用户手改过 → 一律不动。
        """
        ids = [str(i) for i in (ids or []) if i]
        var = getattr(self, "_c_mod", None)
        cur = (var.get().strip() if var is not None else "")
        orig = cur                               # 原配置里的名字（告警文案要用它）
        edited = bool(getattr(self, "_c_mod_user_edited", False))
        # 是否发生了「自动跟随上游换名」：原值非空、不在上游、且不是用户手填的
        switched = bool(ids) and bool(orig) and orig not in ids and not edited
        if ids and var is not None and (not cur or switched):
            var.set(ids[0])                      # 空 / 自动填的已过期 → 落到上游真名
            cur = ids[0]

        multi = len(ids) > 1
        box = getattr(self, "_c_mod_box", None)
        ent = getattr(self, "_c_mod_entry", None)
        # 注意：这里判断"当前用的是哪个控件"必须用 winfo_manager()（'' = 没有布局管理器管它），
        if multi:
            if box is not None:
                vals = list(ids)
                if cur and cur not in vals:
                    vals.append(cur)             # 手填/过期的值也列出来，别显示成空白
                box["values"] = vals
            if ent is not None and ent.winfo_manager():
                ent.pack_forget()
            if box is not None and not box.winfo_manager():
                box.pack(fill="x", ipady=1)
        else:
            if box is not None and box.winfo_manager():
                box.pack_forget()
            if ent is not None and not ent.winfo_manager():
                ent.pack(fill="x", ipady=1)

        note = getattr(self, "_cfg_model_note", None)
        if note is None:
            return
        if not ids:
            note.config(text="还没取到上游模型列表（桥离线或尚未就绪）。", fg=UI["text_hint"])
        elif switched:
            note.config(text=f"⚠ 原配置的 {orig} 已不在上游列表中，已切到 {cur}。",
                        fg=UI["warning"])
        elif cur and cur not in ids:
            note.config(text=f"当前手填的 {cur} 不在上游列表中；桥会按名字匹配，"
                             "匹配不上会自动兜底。", fg=UI["warning"])
        elif multi:
            note.config(text=f"上游共 {len(ids)} 个模型，可下拉选择，也可自行修改。", fg=UI["text_hint"])
        else:
            note.config(text="上游当前只有这一个模型（可自行修改）。", fg=UI["text_hint"])

    def _reload_models(self) -> None:
        """手动刷新模型下拉：直接问桥要一次 /v1/models（桥会强制刷新自己的缓存）。"""
        url = f"http://127.0.0.1:{self.port}/v1/models"
        key = self._c_key.get().strip() or "sk-hainnu"
        note = getattr(self, "_cfg_model_note", None)
        if note is not None:
            note.config(text="正在取上游模型列表…", fg=UI["text_secondary"])

        def work():
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                self.q.put(("models", ids or []))
            except Exception as exc:   # noqa: BLE001
                self.q.put(("models_err", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- 连接配置 / 官方峰谷

    def _save_config_inline(self):
        """主页内嵌「连接配置」面板的保存：把 URL / Key / 模型 / 端口写入 config.json，
        重启代理后才真正生效；当前 config.json 即默认值。"""
        try:
            try:
                port = int(self._c_port.get().strip())
            except ValueError:
                self._cfg_status.config(text="端口必须是数字", fg=UI["danger"])
                return
            d = load_config()
            # 不改上游 URL（config.json 的 upstream 保留原值）
            d["local_api_key"] = self._c_key.get().strip()
            # 「模型」栏里显示的就是上游真名，直接存下来即可
            d["default_model"] = self._c_mod.get().strip()
            d["port"] = port
            CONFIG_PATH.write_text(
                json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if port != self.port:      # 立即联动探活目标 + 本地 URL 文案
                self.port = port
                self.health_url = f"http://127.0.0.1:{port}/health"
                self.health_url_probe = f"http://127.0.0.1:{port}/health?probe=1"
                self._c_url_oai.set(f"http://127.0.0.1:{port}/v1")
                self._c_url_an.set(f"http://127.0.0.1:{port}/v1/messages")
            self._cfg_status.config(text="已保存；重启代理后生效。", fg=UI["success"])
        except Exception as exc:  # noqa: BLE001
            self._cfg_status.config(text=f"保存失败：{exc}", fg=UI["danger"])

    def _greet_suffix(self):
        """顶部问候后缀：按时段问候 +「，姓名/学号。」。
        没有姓名或学号信息时不返回问候（返回空串），顶部就不带问候。"""
        if not self._user_name:
            return ""
        g = time_greeting()
        return f"{g}，{self._user_name}。"

    def _apply_greeting(self):
        """把当前问候+姓名刷到窗口标题（程序名后以空格与竖线分隔）；页眉不再重复。
        无姓名/学号时标题不带问候、也不需要竖线。"""
        gs = self._greet_suffix()
        if gs:
            self.title("Hainnu Proxy · 管理台    |    " + gs)
        else:
            self.title("Hainnu Proxy · 管理台")

    def _load_name_async(self):
        """后台取姓名：优先保留已缓存姓名（如来自超星平台的真实姓名），
        不被 Open WebUI 的学号覆盖；缓存为空时才写后端返回的姓名。"""
        try:
            name = _fetch_user_name()      # Open WebUI 通常返回学号
        except Exception:  # noqa: BLE001
            return
        if not name:
            return
        cached = _load_user_name()
        if cached and cached != name:      # 已有缓存姓名(超星真实姓名)优先，跳过
            if name != self._user_name and cached != self._user_name:
                self._user_name = cached
                self.after_idle(self._apply_greeting)
            return
        try:
            (BASE / "user_name.txt").write_text(name, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if name != self._user_name:
            self._user_name = name
            self.after_idle(self._apply_greeting)

    def _update_peak_state(self):
        """刷新官方峰/谷状态标签：峰值=波峰(红加粗“梁文峰”)，空闲=波谷(绿加粗“梁文谷”)。"""
        pk = beijing_peak()
        self.l_peak_state.config(
            text="梁文峰" if pk else "梁文谷",
            fg=UI["danger"] if pk else UI["success"])

    # ---------------------------------------------------------------- 后台循环

    def _probe_loop(self):
        probe_every = 15          # ~60s 一次上游自检
        n = 0
        while not self._stop:
            n += 1
            url = self.health_url_probe if (n % probe_every == 0) else self.health_url
            timeout = 25 if url is self.health_url_probe else 8
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                self._offline_streak = 0
                self.q.put(("health", data))
            except Exception:  # noqa: BLE001
                # 去抖：连续失败 ≥2 次才上报“离线”，单次超时/抖动不翻转状态
                self._offline_streak += 1
                if self._offline_streak >= 2:
                    self.q.put(("health", None))
            time.sleep(4)

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "health":
                    self._apply_health(payload)
                elif kind == "status":
                    self.txt.config(text=payload, fg=UI["success"])
                elif kind == "status_err":
                    self.txt.config(text=payload, fg=UI["danger"])
                elif kind == "autostart":
                    self.btn_auto.config(text=payload)
                elif kind == "price":
                    self._refresh_price_label()
                    self._update_savings()
                elif kind == "price_err":
                    self._refresh_price_label()
                    self.l_price.config(text="官方价：" + payload, fg=UI["danger"])
                elif kind == "models":
                    self._set_model_choices(payload)
                elif kind == "models_err":
                    note = getattr(self, "_cfg_model_note", None)
                    if note is not None:
                        note.config(text=f"取模型失败：{payload[:70]}", fg=UI["danger"])
        except queue.Empty:
            pass
        self._refresh_usage()
        self._update_peak_state()   # 跨 12:00/18:00 边界自动切换峰/谷
        # 问候只在整小时跨段时才更新（不随 300ms 频率抖动）：
        # 段边界：6(早上好)/9(上午好)/12(中午好)/14(下午好)/18(晚上好)/23(夜深了)
        gh = time.localtime().tm_hour
        if gh != self._last_greet_hour:
            self._last_greet_hour = gh
            self._apply_greeting()
        nowp = time.time()
        if nowp - self._last_prune > PRUNE_INTERVAL:
            self._last_prune = nowp
            prune_usage_log()   # 裁剪超出时间窗的缓存记录
        # 限流自动重启（保守、定时）：到点才重启；到点前逐秒刷新“N 秒后自动重启代理”
        if self._rl_restart_at:
            rem = self._rl_restart_at - time.time()
            if rem > 0:
                self.l_rl.config(text=f"限流：⚠ 限流中，{int(rem)}s 后自动重启代理",
                                 fg=UI["danger"])
            else:
                self._rl_restart_at = 0.0
                self._auto_restart_proxy()
        self.after(300, self._drain)

    # ---------------------------------------------------------------- 健康/状态

    def _apply_health(self, data: dict | None):
        if data is None:
            self._set_status(False)
            self._online_since = None
            self.l_run.config(text=f"代理：离线（http://127.0.0.1:{self.port} 无响应）")
            self.l_uptime.config(text="持续运行：—")
            self.l_speed.config(text="速率：—")
            self.l_speed_live.config(text="实时输出：—")
            self.l_health.config(text="学校后端：无法探测（代理未运行）")
            self.l_models.config(text="模型：—")
            return
        # 收到响应 = 本地桥活着 → 在线。注意：这里的 ok 只反映“学校上游自检”，
        # 不再用它决定在线/离线，避免“学校一抽风本地就被误判离线”。
        self._set_status(True)
        # 桥接上报的持续运行时间；若桥未带该字段（旧桥），用本 GUI 观察到的持续在线时长兜底
        if self._online_since is None:
            self._online_since = time.time()
        up = data.get("uptime") or 0
        self._up_sec = up
        if not up:
            up = time.time() - self._online_since
        # 新版本 /health 不再回传 token_len，只有旧版服务才有；拿不到就不显示这一项
        tok_len = data.get("token_len") or 0
        tok_tail = f" · 令牌长 {tok_len}" if tok_len else ""
        self.l_run.config(
            text=f"代理：在线 · 端口 {self.port}{tok_tail}")
        self.l_uptime.config(text="持续运行：" + fmt_uptime(up))
        rate = recent_tok_rate()              # 近 5 分钟平均 tok/s（含输入）
        self.l_speed.config(text=("速率：近5分钟 " + f"{rate:,.1f} tok/s" if rate > 0
                                  else "速率：—（近5分钟无请求）"))
        # 实时输出速度（桥接流式转发时按内容估算，只计输出，接近 harness 读数）
        _ov = data.get("output_speed", 0.0) or 0.0
        if data.get("output_active"):
            self.l_speed_live.config(text=f"实时输出：{_ov:,.1f} tok/s（出字中）",
                                     fg=UI["primary_dark"])
        elif _ov > 0:
            self.l_speed_live.config(text=f"实时输出：上次 {_ov:,.1f} tok/s",
                                     fg=UI["primary"])
        else:
            self.l_speed_live.config(text="实时输出：—（空闲）", fg=UI["text_hint"])
        # 学校后端状态独立一行：有 probe 结果就刷新（并记住最新结果）；
        # 常规轮询（无 upstream_chat）时**保留**上一次上游自检结果，不覆盖回“—”。
        chat = data.get("upstream_chat")
        if chat:
            if chat.get("ok"):
                dt = chat.get("elapsed")
                self._backend_state = ("学校后端：正常（%ss 出字）" % dt, UI["success"])
            else:
                detail = chat.get("detail") or chat.get("error") or "无"
                self._backend_state = ("学校后端：异常（%s）" % detail[:60], UI["danger"])
        if self._backend_state:
            txt, fg = self._backend_state
            self.l_health.config(text=txt, fg=fg)
        else:
            self.l_health.config(text="学校后端：—（待自检，每~60s自动）",
                                 fg=UI["text_hint"])
        models = data.get("models") or []
        self.l_models.config(text=f"模型：{len(models)} 个 · {', '.join(models[:3])}"
                             + ("..." if len(models) > 3 else ""))
        # 「模型」下拉跟着上游列表一起刷新（数据源就是桥 /health 里的 models）
        self._set_model_choices(models)
        # 限流警告：展示桥接正在自动执行的 429 冷却 / 指数退避重试；状态转变时高亮状态条
        _now = time.time()
        if data.get("rate_limited_now"):
            self._rl_state = self._flash_rate(
                "now", "⚠ 上游限流(429)中，已自动冷却，稍后自动重启代理", UI["danger"])
            self._schedule_rl_restart(data)   # 安排一次“定时”自动重启（保守）
            self.l_rl.config(text="限流：⚠ 限流中，稍后自动重启代理", fg=UI["danger"])
        elif data.get("rate_last_ts") and (_now - float(data["rate_last_ts"])
                                            < max(120, 2 * (data.get("rate_cooldown_secs") or 12))):
            self._rl_state = self._flash_rate(
                "recent", "限流预警：近期触发过 429，已自动错峰＋重试", UI["warning"])
            self.l_rl.config(text="限流：近期触发过 429，已自动错峰＋重试",
                             fg=UI["warning"])
        else:
            self._rl_state = "ok"
            self.l_rl.config(text="限流：正常（429 自动冷却＋指数退避重试已开启）",
                             fg=UI["success"])

    def _flash_rate(self, new: str, msg: str, color: str) -> str:
        """限流状态转变时，让底部状态条高亮提示（避免只有边角小字看不见）。"""
        if new != self._rl_state and new in ("now", "recent"):
            self.txt.config(text=msg, fg=color)
        return new

    def _schedule_rl_restart(self, data: dict):
        """限流时安排一次“按时”自动重启（保守）：
        冷却结束（rate_until_ts）后再过 60s 缓冲才触发重启，绝不立即杀；且
        距上次自动重启不足 5 分钟、或本小时已自动重启 ≥3 次 → 不再安排，避免反复触发限流。"""
        if self._rl_restart_at > time.time():
            return                      # 已有定时任务，不重复/不提前
        until = float(data.get("rate_until_ts") or 0)
        now = time.time()
        hour = int(now // 3600)
        if hour != self._rl_auto_hour:
            self._rl_auto_hour = hour
            self._rl_auto_cnt = 0
        if self._rl_auto_cnt >= 3:
            return
        if self._rl_last_auto and (now - self._rl_last_auto) < 300:
            return
        self._rl_restart_at = until + 60    # 冷却结束后再等 60s，让上游有足够时间放行
        self._rl_auto_cnt += 1

    def _auto_restart_proxy(self):
        """到点执行的自动重启：记录本次时刻，走“先清端口再启动”的稳妥重启。"""
        self._rl_last_auto = time.time()
        self.q.put(("status", "限流冷却后自动重启代理…"))
        self._restart_proxy()

    def _clear_rate_cooldown(self):
        """手动清掉**本地**的限流冷却计时，让桥立刻再试一次。

        学校若已恢复，这样能省掉剩余等待；学校若仍在停用，下一次请求会再收到 429，
        冷却会被重新武装 —— 所以这个按钮只适合「我已经知道它恢复了」的时候点。
        """
        url = f"http://127.0.0.1:{self.port}/_/cooldown?clear=1"
        key = (getattr(self, "_c_key", None).get().strip() if getattr(self, "_c_key", None)
               else "") or "sk-hainnu"

        def work():
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                self.q.put(("status",
                            f"已清除本地冷却（原剩余 {d.get('cleared', 0)}s），下一个请求会立刻发出；"
                            "学校若仍在停用会再收到 429。"))
            except Exception as exc:   # noqa: BLE001
                self.q.put(("status_err", f"清除本地冷却失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _set_status(self, on: bool):
        c = self.status_dot
        c.delete("all")
        color = UI["success"] if on else UI["danger"]
        c.create_oval(3, 3, 13, 13, fill=color, outline="")
        self.status_txt.config(text="在线" if on else "离线", fg=color)

    def _health_now(self):

        def work():
            try:
                with urllib.request.urlopen(self.health_url_probe, timeout=25) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                self.q.put(("health", data))
                self.q.put(("status", "自检完成（自动刷新状态）"))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("health", None))
                self.q.put(("status", f"自检失败：{exc}"))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- 桥接控制

    def _install_deps_then(self, on_ready=None):
        """缺依赖时后台自动安装（等价 0.安装依赖.bat），装完回调 on_ready。
        为什么必须自动：以前只在状态栏写"请运行 0.安装依赖.bat"，结果服务根本
        没起来，用户看到的只是客户端报「目标计算机积极拒绝」，两头对不上。
        安装要几十秒，不能堵在 UI 线程。
        """
        host = any_python()
        if host is None:
            self.q.put(("status_err",
                        "本机找不到任何可用的 Python，无法创建 .venv。"
                        "建议改用分发包 hainnu-proxy.zip（内含便携运行时，解压即用）。"))
            return
        self.q.put(("status", "正在安装依赖（fastapi / uvicorn / httpx），请稍候 …"))
        def work():

            try:
                r = subprocess.run(
                    [str(host), str(BASE / "_deps_check.py"), "--install"],
                    cwd=str(BASE), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=900)
            except Exception as exc:  # noqa: BLE001
                self.q.put(("status_err", f"依赖安装失败：{exc}"))
                return
            if r.returncode == 0:
                self.q.put(("status", "依赖已装齐，正在继续 …"))
                if on_ready is not None:
                    on_ready()
            else:
                self.q.put(("status_err",
                            "依赖自动安装失败（已依次试过清华 / 阿里 / 官方三个源）。"
                            "请手动执行 0.安装依赖.bat 看完整报错，或改用分发包 "
                            "hainnu-proxy.zip（内含便携运行时，解压即用）。"))
        threading.Thread(target=work, daemon=True).start()
    def _proxy_cmd(self, on_ready=None):
        """取一个能跑桥的解释器；一个都没有就自动装依赖（等价 0.安装依赖.bat）。
        on_ready：装完后要重试的动作（例如再走一遍 _start_proxy）；
        传 None 表示只提示、不自动重试（开机自启这类只读查询够用了）。
        """
        py = select_proxy_python()
        if py is not None:
            return py
        self._install_deps_then(on_ready)
        return None
    def _free_proxy_port(self, port: int) -> None:
        """杀掉占用端口的监听进程并等端口释放（等价 2.启动代理.bat 的端口清理）。

        ⚠️ 只在**确认端口上没人健康服务**时才该调用（见 _start_proxy）：
        桥是「async 端点里调同步 httpx」，忙起来连 /health 都不回，
        此时贸然清理 = 把一个健康的桥杀掉，客户端立刻 connection refused。
        """
        for pid in listener_pids(port):
            kill_pid(pid)
        for _ in range(30):                      # 最多约 3 秒等端口释放
            if not listener_pids(port):
                return
            time.sleep(0.1)

    def _probe_health_quiet(self, ladder=(2, 4, 8)) -> bool:
        """耐心阶梯式探活：任一档通过就算健康。

        为什么要阶梯：桥是「async 端点里调同步 httpx」，一个长请求会把事件循环占满，
        期间 /health **完全不响应**。只给 2 秒就下结论，会把「忙」误判成「不在」。
        """
        for timeout in ladder:
            try:
                with urllib.request.urlopen(self.health_url, timeout=timeout) as rf:
                    data = json.loads(rf.read().decode("utf-8", "replace"))
                if data.get("ok"):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _start_proxy(self, force: bool = False):
        """启动桥。

        force=False（默认，点「启动代理」）：**绝不误杀** ——
            端口上已有健康桥 → 什么都不做；
            端口有人在监听但不健康 → 判定它在忙，放弃接管并如实告知（要强换请点「重启代理」）；
            端口确实没人 → 清理残留 + 启动。
        force=True（「重启代理」已经杀过一轮之后）：直接清理端口再起。
        """
        # 缺依赖时先自动装，装完带着同一个 force 参数重试一次
        py = self._proxy_cmd(lambda: self._start_proxy(force))
        if py is None:
            return
        # 与 2.启动代理.bat 保持一致：用 python.exe（而非 pythonw），便于日志可见
        if py and py.name == "pythonw.exe":
            pex = py.with_name("python.exe")
            if pex.exists():
                py = pex

        def work():
            if not force:
                if self._probe_health_quiet():
                    self.q.put(("status",
                                f"代理已在运行（{self.port} 健康检查通过），无需重复启动。"
                                "想换新代码请用「重启代理」"))
                    return
                busy = listener_pids(self.port)
                if busy:
                    # 有人在监听、但健康检查不通过 —— 最可能是它正忙于一个长请求。
                    # 这里**不杀**：杀掉健康但繁忙的桥，正是「桥老是掉」的头号成因。
                    self.q.put(("status_err",
                                f"端口 {self.port} 上有进程在监听（PID {sorted(busy)}），"
                                "但健康检查没通过。桥是单线程的，处理长请求时连 /health 都不回，"
                                "所以这通常只是「忙」而不是「死」。已放弃自动接管以免误杀；"
                                "想强制换新实例请点「重启代理」。"))
                    return
            # 端口上确实没人：才清理（清残留）并启动
            self._free_proxy_port(self.port)
            logf = None
            try:
                logf = open(BASE / "hainnu_proxy_gui.log", "at", encoding="utf-8")
                logf.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] GUI 启动代理 py={py}\n")
                logf.flush()
                p = subprocess.Popen(
                    [str(py), str(BASE / "hainnu_proxy.py")],
                    cwd=str(BASE), stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW)
                self._proxy_log = logf
                self._proxy_proc = p        # 记住自己拉起的子进程，重启时能精确回收
                self.q.put(("status", f"代理启动中（PID {p.pid}）…初始可能要数秒（联网取令牌/模型）"))
                # 自验证：最多约 30 秒持续探测健康；桥在慢网络时才刚 [booting]，别因 6 秒就误判失败
                pinged = 0
                for _ in range(60):
                    time.sleep(0.5)
                    try:
                        with urllib.request.urlopen(self.health_url, timeout=3) as rf:
                            data = json.loads(rf.read().decode("utf-8", "replace"))
                        if data.get("ok"):
                            self.q.put(("status", f"代理已在线（PID {p.pid}）。"))
                            return
                        pinged += 1   # 端口已响应但探活未过（如学校后端探测较慢），继续等
                    except Exception:  # noqa: BLE001
                        pass
                tail = ""
                try:
                    with open(BASE / "hainnu_proxy_gui.log", "r", encoding="utf-8",
                              errors="replace") as f:
                        tail = "\n".join(f.readlines()[-15:]).strip()[-600:]
                except Exception:  # noqa: BLE001
                    tail = ""
                self.q.put(("status_err",
                            "代理启动后健康检查仍失败，日志："
                            + (tail or "（无日志输出，可能是依赖缺失或端口仍被占用）")))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("status_err", f"启动失败：{exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _reap_own_proxy(self) -> int:
        """回收「我们自己 Popen 出来的那个子进程」。

        它可能已经卡死在半路（进程还在、但 8787 早就不监听了）——
        这种孤儿只按端口是杀不到的，必须靠记下来的句柄兜底。
        """
        proc = getattr(self, "_proxy_proc", None)
        self._proxy_proc = None
        if proc is None or proc.poll() is not None:
            return 0
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        logf = getattr(self, "_proxy_log", None)
        if logf is not None:
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass
            self._proxy_log = None
        return 1

    def _stop_proxy(self):
        """停止代理：只杀**监听本端口**的进程 + 自己拉起的子进程，不做命令行全杀。"""
        n = stop_proxy_processes(self.port)
        n = max(0, n) + self._reap_own_proxy()
        self.q.put(("status", f"已停止代理进程（{n} 个）"))

    def _restart_proxy(self):
        """手动重启：先精确停掉（端口监听者 + 自己的子进程），再 force 启动。

        以前这里是 `stop_proxy_processes()` 全杀 —— 会连坐机器上别人正在跑的桥实例。
        """
        n = max(0, stop_proxy_processes(self.port)) + self._reap_own_proxy()
        self.q.put(("status", f"已停止 {n} 个桥进程，正在拉起新的…"))
        time.sleep(0.8)
        self._start_proxy(force=True)

    def _init_autostart_label(self):
        """启动时读真实自启状态，把按钮显示成「已设开机自启 / 启用开机自启」。"""
        py = self._proxy_cmd()
        if py is None:
            return
        try:
            r = subprocess.run([str(py), str(BASE / "autostart.py"), "--status"],
                               cwd=str(BASE), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            label = "已设开机自启" if ("已设置" in out) else "启用开机自启"
            self.after_idle(lambda: self.btn_auto.config(text=label))
        except Exception:  # noqa: BLE001
            self.after_idle(lambda: self.btn_auto.config(text="启用开机自启"))

    # ---------------------------------------------------------------- 开机自启

    def _toggle_autostart(self):
        py = self._proxy_cmd()
        if py is None:
            return

        def work():
            try:
                r = subprocess.run([str(py), str(BASE / "autostart.py"), "--status"],
                                   cwd=str(BASE), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                out = (r.stdout or "") + (r.stderr or "")
                installed = ("已设置" in out)
                arg = "--uninstall" if installed else "--install"
                r2 = subprocess.run([str(py), str(BASE / "autostart.py"), arg],
                                    cwd=str(BASE), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                done = (r2.stdout or "") + (r2.stderr or "")
                # 开机自启按钮文案：设置后→“已设开机自启”；再次点击关闭→恢复“启用开机自启”
                label = "启用开机自启" if installed else "已设开机自启"
                self.q.put(("autostart", label))
                self.q.put(("status", done.strip()[:160] or label))
                if installed:
                    self.q.put(("health", None))  # 触发刷新
            except Exception as exc:  # noqa: BLE001
                self.q.put(("status_err", f"自启操作失败：{exc}"))
                self.q.put(("autostart", "启用开机自启"))
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- 获取令牌

    def _get_token(self, legacy=False):
        py = Path(sys.executable)

        def work():
            check = subprocess.run([str(py), "-c", "import playwright"],
                                   cwd=str(BASE), capture_output=True)
            if check.returncode != 0:
                self.q.put(("status", "首次使用令牌工具，正在按需安装 playwright（约110MB）…"))
                inst = subprocess.run(
                    [str(py), "-m", "pip", "install", "-q",
                     "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "playwright"],
                    cwd=str(BASE), capture_output=True)
                if inst.returncode != 0:
                    tail = (inst.stderr or inst.stdout or b"").decode("utf-8", "replace")[-200:]
                    self.q.put(("status_err", f"playwright 安装失败：{tail}"))
                    return
            cmd = [str(py), str(BASE / "get_token.py")]
            if legacy:
                cmd.append("--legacy")
            subprocess.Popen(cmd, cwd=str(BASE),
                             stdout=DEVNULL, stderr=DEVNULL, creationflags=CREATE_NO_WINDOW)
            msg = ("备用登录已启动（原有聊天链路）：请在弹出的 Chrome 窗口登录；成功后自动写入 token.txt"
                   if legacy else
                   "令牌获取已启动：请在弹出的 Chrome 窗口登录；成功后自动写入 token.txt 与姓名")
            self.q.put(("status", msg))
        threading.Thread(target=work, daemon=True).start()

    def _get_token_legacy(self):
        self._get_token(legacy=True)

    # ---------------------------------------------------------------- 流量图表

    def _paint_window_chips(self) -> None:
        """重画时间窗分段块的选中态（飞书式：选中主色实底白字）。"""
        cur = self.var_win.get()
        for name, chip in self._win_chips.items():
            if name == cur:
                chip.config(bg=UI["primary"], fg="#FFFFFF",
                            highlightbackground=UI["primary"])
            else:
                chip.config(bg=UI["card"], fg=UI["text_regular"],
                            highlightbackground=UI["border"])

    def _hover_window(self, name: str, on: bool) -> None:
        """未选中的块悬停时给一层浅灰底，选中块不响应悬停。"""
        if name == self.var_win.get():
            return
        self._win_chips[name].config(bg=UI["hover"] if on else UI["card"])

    def _set_window(self, name: str) -> None:
        """点选时间窗：更新变量、重画分段块、刷新曲线/表格。"""
        self.var_win.set(name)
        self._paint_window_chips()
        self._on_window()

    def _on_window(self):
        for name, sec, nb in WINDOWS:
            if self.var_win.get() == name:
                self._window = (sec, nb)
                break
        self._tbl_stamp = None
        self._refresh_usage()

    def _paint_switch(self) -> None:
        """飞书式滑动开关：关=曲线，开=表格。"""
        c = self._sw
        c.delete("all")
        on = self.var_view.get() == "table"
        track = UI["primary"] if on else UI["border"]
        c.create_oval(1, 1, 21, 21, fill=track, outline="")
        c.create_oval(19, 1, 39, 21, fill=track, outline="")
        c.create_rectangle(11, 1, 29, 21, fill=track, outline="")
        kx = 20 if on else 2
        c.create_oval(kx, 2, kx + 18, 20, fill="#FFFFFF", outline="")
        self.l_view_chart.config(fg=UI["text_main"] if not on else UI["text_hint"])
        self.l_view_table.config(fg=UI["text_main"] if on else UI["text_hint"])

    def _set_usage_view(self, view: str) -> None:
        """标题行右侧开关：曲线 ↔ 表格。时间窗筛选两边共用。"""
        if view not in ("chart", "table"):
            return
        self.var_view.set(view)
        self._paint_switch()
        if view == "chart":
            self.tbl_frame.pack_forget()
            self.canvas.pack(fill="both", expand=True, pady=(4, 0))
            self.update_idletasks()
            self._refresh_usage()
        else:
            self.canvas.pack_forget()
            self.tbl_frame.pack(fill="both", expand=True, pady=(4, 0))
            self._tbl_stamp = None
            self._fill_usage_table()

    def _load_ds_icon(self):
        """DeepSeek 系列模型的 18px 鲸鱼图标；Pillow 不可用或文件缺失则不用。"""
        p = BASE / "deepseek.png"
        if not p.exists():
            return None
        try:
            from PIL import Image, ImageTk
            im = Image.open(p)
            self._ico_ds_ref = ImageTk.PhotoImage(im)
            return self._ico_ds_ref
        except Exception:  # noqa: BLE001
            return None

    def _row_cost(self, rec: dict) -> str:
        p = self.price or dict(FLASH_PRICE_FALLBACK)
        peak = beijing_peak(rec.get("ts"))
        hp = (p["in_hit_peak"] if peak else p["in_hit_off"]) / 1e6
        mp = (p["in_miss_peak"] if peak else p["in_miss_off"]) / 1e6
        out_p = (p["out_peak"] if peak else p["out_off"]) / 1e6
        pin = int(rec.get("prompt") or 0)
        pout = int(rec.get("completion") or 0)
        ch = rec.get("cache_hit")
        if ch is None:
            ratio = CACHE_HIT_RATIO_DEFAULT
            yuan = pin * (ratio * hp + (1 - ratio) * mp) + pout * out_p
        else:
            ch = int(ch or 0)
            cm = int(rec.get("cache_miss") or 0)
            if ch + cm <= 0:
                cm = pin
            yuan = ch * hp + cm * mp + pout * out_p
        return f"¥{yuan:.4f}" if yuan < 0.01 else f"¥{yuan:.2f}"

    def _tbl_pages(self) -> int:
        return max(1, (len(self._tbl_recs) + TABLE_PAGE - 1) // TABLE_PAGE)

    def _tbl_goto(self, page: int) -> None:
        self._tbl_page = max(0, min(int(page), self._tbl_pages() - 1))
        self._render_usage_page()

    def _fill_usage_table(self) -> None:
        """加载当前时间窗记录；翻页只重画当前 50 条。"""
        sec = self._window[0]
        cutoff = calendar_anchor(sec)
        if cutoff is None:
            cutoff = time.time() - sec
        try:
            st = USAGE_LOG.stat()
            stamp = (st.st_mtime, st.st_size, cutoff)
        except Exception:  # noqa: BLE001
            stamp = None
        if stamp is not None and stamp == self._tbl_stamp:
            return
        self._tbl_stamp = stamp
        recs = []
        if stamp is not None:
            try:
                with open(USAGE_LOG, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if (rec.get("ts") or 0) >= cutoff:
                            recs.append(rec)
            except Exception:  # noqa: BLE001
                recs = []
        recs.reverse()
        self._tbl_recs = recs
        self._tbl_page = 0
        self._render_usage_page()

    def _render_usage_page(self) -> None:
        tree = self.usage_tree
        tree.delete(*tree.get_children())
        recs = self._tbl_recs
        pages = self._tbl_pages()
        page = max(0, min(self._tbl_page, pages - 1))
        self._tbl_page = page
        start = page * TABLE_PAGE
        chunk = recs[start:start + TABLE_PAGE]
        ico = self._ico_ds
        for rec in chunk:
            ts = rec.get("ts") or 0
            prompt = int(rec.get("prompt") or 0)
            completion = int(rec.get("completion") or 0)
            hit, miss = rec.get("cache_hit"), rec.get("cache_miss")
            if hit is None and miss is None:
                cache_s, rate_s = "—", "—"
            else:
                hit = int(hit or 0)
                den = hit + int(miss or 0)
                cache_s = f"{hit:,}"
                rate_s = f"{hit / den * 100:.1f}%" if den else "—"
            ms = rec.get("ms")
            ms_s = f"{float(ms) / 1000:.2f}s" if ms is not None else "—"
            model = str(rec.get("model") or "—")
            kw = {}
            if ico is not None and "deepseek" in model.lower():
                kw["image"] = ico
            tree.insert("", "end", text=model, values=(
                time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                rec.get("effort") or "—",
                f"{prompt:,} / {cache_s} / {completion:,}",
                ms_s, self._row_cost(rec), rate_s), **kw)
        self.l_tbl_page.config(text=f"第 {page + 1} / {pages} 页")
        self.l_tbl_count.config(text=f"共 {len(recs)} 条 · 每页 {TABLE_PAGE}")
        self.btn_tbl_prev.config(state=("disabled" if page <= 0 else "normal"))
        self.btn_tbl_next.config(state=("disabled" if page >= pages - 1 else "normal"))

    @staticmethod
    def _avg_unit_sec(sec: int) -> int:
        """平均的统计单位（秒）：1小时→每分钟(60s)、24小时→每小时(3600s)、周/月→每天(86400s)。"""
        return {3600: 60, 86400: 3600, 604800: 86400, 2592000: 86400}.get(sec, 3600)

    @staticmethod
    def _unit_name(sec: int) -> str:
        if sec >= 604800:
            return "每天"                  # 1周/1月 → 平均每天
        if sec >= 86400:
            return "每小时"                # 24小时 → 每小时
        return "每分钟"                    # 1小时 → 每分钟

    @staticmethod
    def _run_display(unit_sec: int, run: float) -> str:
        if unit_sec >= 86400:
            return f"{run:.0f}天"
        if unit_sec >= 3600:
            return f"{run:.0f}小时"
        return f"{run:.0f}分钟"

    def _elapsed_sec(self, sec: int) -> float:
        """窗口内“实际已流逝”的秒数（平均分母的封顶）：周/月按日历起点到今天
        （算进没使用的日子，但**不计今天之后**）；1小时/24小时是整段滑动窗。与运行时长相夹取小值。"""
        up = getattr(self, "_up_sec", 0) or 0
        if not up and getattr(self, "_online_since", None):
            up = time.time() - self._online_since
        if not up or up <= 0:
            up = sec
        if sec < 86400:                    # 1h / 24h：整段滑动窗
            span = sec
        else:                              # 1周/1月：日历起点(周一0点/1号0点) → 今天，不含未来
            a = calendar_anchor(sec)
            span = (time.time() - a) if a else sec
        return min(up, span)

    def _avg_running(self, sec: int, unit_sec: int) -> float:
        """平均分母：窗口内实际运算时长折算成的单位数（每分钟/每小时/每天）。不足1单位按1算。"""
        units = self._elapsed_sec(sec) / unit_sec
        return units if units >= 1.0 else 1.0

    def _avg_text(self, sec: int, unit_sec: int,
                  total_in: int, total_out: int, count: int) -> str:
        """按图表单位、且只按“运行时”统计的均值（输入/输出 tokens 与请求数）。"""
        run = self._avg_running(sec, unit_sec)
        return (f"　均值({self._unit_name(sec)}·按运行时{self._run_display(unit_sec, run)}): "
                f"输入 {fmt_tokens(total_in / run)} · 输出 {fmt_tokens(total_out / run)} · "
                f"请求 {count / run:.2f}")

    def _refresh_usage(self):
        sec, nb = self._window
        unit_sec = self._avg_unit_sec(sec)   # 平均单位：1h/分、24h/时、周月/天
        bins, bout, total_in, total_out, count, brate, (hit_t, miss_t) = agg_usage(sec, nb)
        self.l_sum.config(
            text=(f"输入 {fmt_tokens(total_in)} · 输出 {fmt_tokens(total_out)} · "
                  f"合计 {fmt_tokens(total_in + total_out)} tokens · {count} 次请求"
                  + self._avg_text(sec, unit_sec, total_in, total_out, count)))
        # 统计瓦片：累计 / 今日 tokens（历史全部；今日为本地 0 点起）
        self.l_tokens_total.config(text=fmt_tokens(cumulative_tokens()))
        self.l_tokens_total_sub.config(text=f"共 {cumulative_tokens():,} tokens")
        _today = today_tokens()
        self.l_tokens_today.config(text=fmt_tokens(_today))
        self.l_tokens_today_sub.config(text=f"共 {_today:,} tokens")
        # 缓存命中率（今日 / 累计）。两条口径都用 calendar_anchor 的「0 点」起点。
        _ca = cache_stats_since(0.0)
        if _ca["known_requests"]:
            _ct = cache_stats_since(calendar_anchor(86400) or 0.0)
            self.l_cache.config(text=fmt_tokens(_ct["hit_tokens"]))
            self.l_cache_sub.config(
                text=f"命中率 {(_ct['rate'] or 0):.1f}% · 累计 {fmt_cache_line(_ca)}")
        else:
            self.l_cache.config(text="—")
            self.l_cache_sub.config(text="暂无缓存信息（重启代理后记录）")
        if self.var_view.get() == "table":
            self._fill_usage_table()
        else:
            self._draw_line(bins, bout, brate, (hit_t, miss_t))
        self._update_savings()
        self._refresh_price_label()

    def _draw_empty(self):
        c = self.canvas
        c.delete("all")
        self._chart_data = None
        self._chart_hover_i = None
        self._render_hover()      # 清掉竖线/浮窗
        W = max(c.winfo_width(), 400)
        H = max(c.winfo_height(), 160)
        c.create_text(W / 2, H / 2, text="正在加载用量数据…", fill=UI["text_hint"])

    def _draw_line(self, bins, bout, brate=None, rate_totals=None):
        """折线图：输入(蓝)/输出(橙)/缓存命中率(紫) —— 三条同一套画法。

        每条各自独立量程、竖直色柱填充 + 平滑曲线 + 自己的右侧轴（跟输出轴完全同一套代码）。
        命中率是百分比，量程就取 0~100。某桶没有带缓存信息的记录时不画那一段
        （老记录没有 cache_hit 字段，当 0 画会造假谷底）。
        """
        c = self.canvas
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 50 or H < 50:
            W, H = 860, 240
        c.delete("all")
        sec = self._window[0]
        # 右侧要放两列刻度：输出 + 缓存命中率（跟输出轴同一套画法，各占一列）
        pad_l, pad_r, pad_t, pad_b = 80, 96, 26, 44
        ch = H - pad_t - pad_b
        cw = W - pad_l - pad_r
        n = len(bins)
        hbin = max(bins[:-1]) if n > 1 else 0
        hbout = max(bout[:-1]) if n > 1 else 0
        in_peak = hbin or (bins[-1] if n else 0)
        out_peak = hbout or (bout[-1] if n else 0)
        if not (in_peak or out_peak):   # 输入/输出均为空 → 暂无数据
            c.create_text(pad_l + cw / 2, pad_t + ch / 2,
                          text="（该时间窗暂无用量数据）", fill=UI["text_hint"], font=F_BODY)
            return
        # 双纵轴：输入(左) 与 输出(右) 各自独立量程，输出不再被输入的量级压扁。
        # 量程只看“已定历史桶”(排除正在增长的最新桶) → 新数据只抬高最右桶，旧曲线保持原样。
        top_in = nice_ceil(in_peak * 1.08) if in_peak > 0 else 1.0
        # 输出量程再翻倍：输出柱高约为输入同数值下的一半，避免“看起来输出比输入还多”
        top_out = (nice_ceil(out_peak * 1.08) if out_peak > 0 else 1.0) * 2
        base = pad_t + ch
        x_i = lambda i: pad_l + (i + 0.5) * (cw / n)
        y_in = lambda v: pad_t + ch - (v / top_in) * (ch - 16)
        y_out = lambda v: pad_t + ch - (v / top_out) * (ch - 16)
        # ---- 缓存命中率(紫)：跟另两条一样各自独立量程，百分比就取 0~100 ----
        y_rate = lambda v: pad_t + ch - (v / 100.0) * (ch - 16)
        # 高密度、保单调、不越界的平滑曲线（单调三次插值）
        pin = [(x, min(max(y, pad_t), base)) for x, y in smooth_line(
            [(x_i(i), y_in(bins[i])) for i in range(n)], 32)]
        pout = [(x, min(max(y, pad_t), base)) for x, y in smooth_line(
            [(x_i(i), y_out(bout[i])) for i in range(n)], 32)]
        # 命中率折线：按「有数据的连续段」拆开，缺数据的桶不画（同一条曲线，不额外加说明文字）
        rsegs_raw, cur = [], []
        for i, v in enumerate(brate or []):
            if v is None:
                if len(cur) > 1:
                    rsegs_raw.append(cur)
                cur = []
            else:
                cur.append((x_i(i), min(max(y_rate(v), pad_t), base)))
        if len(cur) > 1:
            rsegs_raw.append(cur)
        rsegs = [smooth_line(s, 32) for s in rsegs_raw]
        COLOR_RATE = UI["chart_rate"]
        # 抗锯齿渲染：垂直色柱填充 + 平滑曲线（Pillow 3×超采样 + LANCZOS 缩回）
        if ensure_pillow():
            img = self._render_chart(pin, pout, pad_l, pad_l + cw, pad_t,
                                     base, UI["chart_fill_in"], UI["chart_fill_out"], UI["primary"], UI["chart_out"],
                                     rsegs, COLOR_RATE, UI["chart_fill_rate"])
            self._photo_ref = self._make_photo(img)
            c.create_image(pad_l, pad_t, image=self._photo_ref, anchor="nw")
        else:
            # 兜底：无 Pillow 时退化为普通平滑折线（无抗锯齿、无垂直色柱）
            c.create_line(pin, width=2, fill=UI["primary"])
            c.create_line(pout, width=2, fill=UI["chart_out"])
            for seg in rsegs:
                c.create_line(seg, width=2, fill=COLOR_RATE)
        # 左纵轴(输入)刻度 + 基线
        c.create_line(pad_l, base, pad_l + cw, base, fill=UI["border"])
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            gv = f * top_in
            gy = pad_t + (1 - f) * (ch - 16)
            c.create_line(pad_l, gy, pad_l + cw, gy,
                          fill=(UI["border"] if f == 0 else UI["divider"]))
            if f > 0:
                c.create_text(pad_l - 8, gy, text=fmt_tokens(gv), anchor="e",
                              fill=UI["text_hint"], font=F_SMALL)
        c.create_text(pad_l - 8, base, text="0", anchor="e",
                      fill=UI["text_hint"], font=F_SMALL)
        # 右纵轴(输出)：右侧边框 + 独立刻度（橙色，对应输出曲线）
        rx = pad_l + cw
        c.create_line(rx, pad_t, rx, base, fill=UI["border"])
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            gv = f * top_out
            gy = pad_t + (1 - f) * (ch - 16)
            if f > 0:
                c.create_text(rx + 5, gy, text=fmt_tokens(gv), anchor="w",
                              fill=UI["chart_out"], font=F_SMALL)
        c.create_text(rx + 5, base, text="0", anchor="w",
                      fill=UI["chart_out"], font=F_SMALL)
        # 右起第二条轴：缓存命中率（紫，0~100%）。跟输出轴完全同一套画法。
        rx2 = rx + 54
        c.create_line(rx2, pad_t, rx2, base, fill=UI["border"])
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            gv = f * 100.0
            gy = pad_t + (1 - f) * (ch - 16)
            if f > 0:
                c.create_text(rx2 + 5, gy, text=f"{gv:.0f}%", anchor="w",
                              fill=UI["chart_rate"], font=F_SMALL)
        c.create_text(rx2 + 5, base, text="0%", anchor="w",
                      fill=UI["chart_rate"], font=F_SMALL)
        # 三轴名：放在绘图区上沿的留白里（pad_t-12，刻度/图例都在其下方），不遮挡量尺
        c.create_text(pad_l / 2 + 10, pad_t - 12, text="输入(左轴)", fill=UI["primary"],
                      font=F_SMALL_B)
        c.create_text(rx + 5, pad_t - 12, text="输出", fill=UI["chart_out"],
                      font=F_SMALL_B)
        c.create_text(rx2 + 5, pad_t - 12, text="命中率", fill=UI["chart_rate"],
                      font=F_SMALL_B)
        # 图例：三条各占一行，样式完全一致
        c.create_line(pad_l + 6, pad_t + 6, pad_l + 26, pad_t + 6, fill=UI["primary"], width=2)
        c.create_text(pad_l + 30, pad_t + 6, text="输入", anchor="w",
                      fill=UI["text_secondary"], font=F_SMALL)
        c.create_line(pad_l + 6, pad_t + 18, pad_l + 26, pad_t + 18, fill=UI["chart_out"], width=2)
        c.create_text(pad_l + 30, pad_t + 18, text="输出", anchor="w",
                      fill=UI["text_secondary"], font=F_SMALL)
        c.create_line(pad_l + 6, pad_t + 30, pad_l + 26, pad_t + 30,
                      fill=COLOR_RATE, width=2)
        c.create_text(pad_l + 30, pad_t + 30, text="缓存命中率", anchor="w",
                      fill=UI["text_secondary"], font=F_SMALL)
        # 横轴时间点：锚定窗标绝对时段起点；1h 滑动窗标相对当前时刻的时段起点
        now = time.time()
        step = int(sec / n)
        fmt = "%H:%M" if sec <= 86400 else "%m-%d"
        t = n if n <= 8 else 6                    # 24h 144桶→取6个点
        idxs = sorted({int(round(k * (n - 1) / (t - 1))) for k in range(t)})
        for i in idxs:
            # 1h 锚到最近 N 个整分钟槽(整块平移)；24h/周/月锚到日历边界。
            # 因此所有窗口都用同一套墙钟起点，历史标签不再随 now 爬动。
            tzoff = time.localtime().tm_gmtoff
            ks = calendar_anchor(sec)
            k_n = int((now + tzoff) // step)
            if ks is not None:
                k_start = int((ks + tzoff) // step)       # 日历边界槽(0点/周一/1号)
            else:
                k_start = k_n - (n - 1)                   # 1h：最近 N-1 个整分钟槽
            start = (k_start + i) * step - tzoff          # 第 i 桶的墙钟起点(UTC epoch)
            lbl = time.strftime(fmt, time.localtime(start))
            c.create_text(x_i(i), base + 12, text=lbl, anchor="n",
                          fill=UI["text_secondary"], font=F_SMALL)
        # 底部汇总（跟原来同一行，同一种分隔符风格）
        _sumb = ("输入 " + fmt_tokens(sum(bins)) + " / 输出 " + fmt_tokens(sum(bout)))
        if rate_totals and sum(rate_totals) > 0:
            _h, _m = rate_totals
            _sumb += f" / 命中率 {_h / (_h + _m) * 100:.1f}%"
        c.create_text(pad_l + cw / 2, base + 36, text=_sumb,
                      fill=UI["text_secondary"], font=F_SMALL)
        # 悬停提示数据（含图内几何信息）+ 绑定鼠标事件：灰色竖线跟随鼠标；最邻近桶无数据时只画竖线
        self._chart_data = (bins, bout, brate, n, pad_l, cw, step, sec, pad_t, base)
        c.bind("<Motion>", self._chart_hover)
        c.bind("<Leave>", self._chart_leave)
        self._render_hover()          # 重绘后复现当前的竖线/浮窗（否则被 delete("all") 抹掉）

    def _chart_hover(self, event):
        c = self.canvas
        d = getattr(self, "_chart_data", None)
        x = event.x
        self._chart_hover_x = x
        self._chart_hover_y = event.y
        self._chart_hover_i = None
        if d:
            bins, bout, brate, n, pad_l, cw, step, sec, pad_t, base = d
            if cw > 0 and n > 0:
                i = round((x - pad_l) / (cw / n) - 0.5)
                if 0 <= i < n:
                    self._chart_hover_i = i
        self._render_hover()

    def _render_hover(self):
        """按当前悬停状态画：1px 灰色竖线（始终跟随鼠标）+ 数据浮窗（仅当该桶有数据）。"""
        c = self.canvas
        c.delete("hline")
        c.delete("tip")
        d = getattr(self, "_chart_data", None)
        i = getattr(self, "_chart_hover_i", None)
        if not d or i is None:
            return
        bins, bout, brate, n, pad_l, cw, step, sec, pad_t, base = d
        if cw <= 0 or n <= 0 or not (0 <= i < n):
            return
        # 竖线紧随鼠标 X（不再吸到桶中心），跨越图区
        mx = getattr(self, "_chart_hover_x", pad_l + (i + 0.5) * (cw / n))
        c.create_line(mx, pad_t, mx, base, fill=UI["text_hint"], width=1, tags="hline")
        _rt = brate[i] if (brate and i < len(brate)) else None
        if not (bins[i] or bout[i] or _rt is not None):   # 该桶完全没数据 → 不显示浮窗
            return
        # 该桶的墙钟时间区间（时间区间 输入量 输出量）
        tzoff = time.localtime().tm_gmtoff
        now = time.time()
        ks = calendar_anchor(sec)
        k_n = int((now + tzoff) // step)
        k_start = int((ks + tzoff) // step) if ks is not None else k_n - (n - 1)
        s0 = (k_start + i) * step - tzoff
        fmt = "%m-%d %H:%M" if (step <= 3600 and sec > 86400) else (
            "%H:%M" if step <= 3600 else "%m-%d")
        t1 = time.strftime(fmt, time.localtime(s0))
        t2 = time.strftime(fmt, time.localtime(s0 + step))
        txt = (f"{t1} ~ {t2}\n输入 {fmt_tokens(bins[i])}  输出 {fmt_tokens(bout[i])}"
               + (f"  命中率 {_rt:.1f}%" if _rt is not None else "  命中率 —"))
        # 浮窗竖向用鼠标 Y（之前误用 X，曲线靠右侧时浮窗被画出画布外而看不见）
        my = getattr(self, "_chart_hover_y", (pad_t + base) / 2)
        # 深色小底 + 白字，光标右上方（不超出画布右缘）
        ww = c.winfo_width()
        bw = 250
        bx = min(mx + 14 + bw, ww - 4) - bw
        c.create_rectangle(bx, my - 26, bx + bw, my + 2, fill=UI["text_main"],
                           outline=UI["text_main"], tags="tip")
        c.create_text(bx + 6, my - 12, text=txt, anchor="w",
                      fill="#FFFFFF", font=F_SMALL, tags="tip")

    def _chart_leave(self, event=None):
        self._chart_hover_i = None
        self._render_hover()

    def _render_chart(self, pin, pout, xl, xr, yt, base_y,
                      fill_in, fill_out, color_in, color_out,
                      rsegs=None, color_rate=UI["chart_rate"], fill_rate=UI["chart_fill_rate"]):
        """Pillow 3×超采样 + LANCZOS 缩回：垂直色柱填充(两侧/峰谷为竖直墙) + 抗锯齿平滑曲线。

        三条曲线同一套画法：各自色柱填充 + 平滑曲线。`rsegs` 是命中率的折线段列表
        （可能多段：缺数据的桶处不画），填充只在该段范围内进行，段外自然留白。
        """
        from PIL import Image, ImageDraw
        S = 3
        pw = int(xr - xl)
        ph = int(base_y - yt)
        if pw <= 0 or ph <= 0:
            return None
        bg = self.canvas["bg"] or "#ffffff"
        img = Image.new("RGB", (S * pw, S * ph), bg)
        d = ImageDraw.Draw(img)
        xs = [p[0] for p in pin]
        yin = [p[1] for p in pin]
        yout = [p[1] for p in pout]

        def interp(xs_arr, arr, xf):
            lo, hi = 0, len(xs_arr) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if xs_arr[mid] <= xf:
                    lo = mid
                else:
                    hi = mid
            a, b = xs_arr[lo], xs_arr[hi]
            if b == a:
                return arr[lo]
            u = (xf - a) / (b - a)
            return arr[lo] + u * (arr[hi] - arr[lo])

        def vert_fill(xs_arr, arr, fill, only_inside=False):
            for px in range(pw):
                xf = xl + px
                if only_inside and (xf < xs_arr[0] or xf > xs_arr[-1]):
                    continue                      # 段外不填（命中率缺数据处留白）
                yi = interp(xs_arr, arr, xf)
                if yi >= base_y - 0.5:
                    continue
                d.line([(S * px, S * (base_y - yt)), (S * px, S * max(yi - yt, 0))],
                       fill=fill, width=S)

        vert_fill(xs, yin, fill_in)
        vert_fill(xs, yout, fill_out)
        for seg in (rsegs or []):
            vert_fill([p[0] for p in seg], [p[1] for p in seg], fill_rate, True)

        def curve(pts, color):
            sp = [(S * (x - xl), S * (y - yt)) for x, y in pts]
            d.line(sp, fill=color, width=2 * S, joint="curve")
        curve(pin, color_in)
        curve(pout, color_out)
        for seg in (rsegs or []):
            curve(seg, color_rate)
        return img.resize((pw, ph), Image.LANCZOS)

    def _make_photo(self, img):
        from PIL import ImageTk
        self._photo_ref = ImageTk.PhotoImage(img)
        return self._photo_ref

    # ---------------------------------------------------------------- 价格/省钱

    def _measured_hit_ratio(self) -> float | None:
        """全量日志里**实测**的缓存命中占比（0~1）；还没有带缓存信息的记录时返回 None。

        只算「有 cache_hit 字段」的记录（桥重启后写入的），老记录不计入 —— 否则会把
        实测值稀释成一个既不是预设也不是事实的怪数字。
        """
        try:
            st = cache_stats_since(0.0)
        except Exception:  # noqa: BLE001
            return None
        tot = st["hit_tokens"] + st["miss_tokens"]
        if tot <= 0:
            return None
        return st["hit_tokens"] / tot

    def current_price(self) -> tuple[float, float, bool]:
        """返回 (混合输入价, 输出价, 是否高峰)，元/百万 tokens。

        混合输入价 = 命中占比×命中价 + 未命中占比×未命中价。占比用**实测**值，
        没有缓存数据时才退回预设的 97%（命中/未命中价差 50 倍，估错代价很大）。
        """
        p = self.price or dict(FLASH_PRICE_FALLBACK)
        peak = beijing_peak()
        hp = p["in_hit_peak"] if peak else p["in_hit_off"]
        mp = p["in_miss_peak"] if peak else p["in_miss_off"]
        ratio = self._measured_hit_ratio()
        if ratio is None:
            ratio = CACHE_HIT_RATIO_DEFAULT
        in_p = ratio * hp + (1 - ratio) * mp
        out_p = p["out_peak"] if peak else p["out_off"]
        return in_p, out_p, peak

    def _price_origin(self) -> str:
        """价格来源 + 新鲜度，例如「官方页 · 3 小时前」。"""
        if not self.price_fetched_at:
            return "内置基准（尚未联网）"
        age = time.time() - self.price_fetched_at
        if age < 90:
            when = "刚刚"
        elif age < 3600:
            when = f"{int(age // 60)} 分钟前"
        elif age < 86400:
            when = f"{int(age // 3600)} 小时前"
        else:
            when = f"{int(age // 86400)} 天前"
        return f"{self.price_source or '未知来源'} · {when}"

    def _refresh_price_label(self):
        in_p, out_p, peak = self.current_price()
        # 混合输入价是「按命中占比加权」来的 —— 把用的是实测还是预设标出来，避免误读
        _r = self._measured_hit_ratio()
        _src = (f"按实测命中 {_r * 100:.1f}%" if _r is not None
                else f"按预设命中 {CACHE_HIT_RATIO_DEFAULT * 100:.0f}%")
        when = "高峰时段" if peak else "空闲时段"
        when += "（北京" + time.strftime("%H:%M", time.gmtime(time.time() + 8 * 3600)) + "）"
        self.l_price.config(
            text=f"官方价(deepseek-flash)：混合输入 ¥{in_p:.3g}/百万（{_src}）· 输出 ¥{out_p:g}/百万",
            fg=UI["text_secondary"])
        self.l_price_note.config(text=f"{when} · 来源 {self._price_origin()}", fg=UI["text_secondary"])

    def _persist_savings(self):
        try:
            SAVINGS_STATE.write_text(json.dumps({
                "saved_yuan": round(self.saved_yuan, 6),
                "last_ts": self.last_ts,
                "price": self.price,
                "price_fetched_at": self.price_fetched_at,
                "price_source": self.price_source,
            }), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _update_savings(self):
        """按**实际缓存命中**逐条计费（之前是按固定 97% 命中率估的）。

        命中/未命中的输入价差 50 倍（0.02 vs 1.0 元/百万），所以必须把 prompt 拆成
        命中与未命中分别乘价，不能再用一个混合单价糊过去。

        口径沿用原来的**增量**方式：只累加 `ts > self.last_ts` 的新记录，
        已计入的历史不回溯重算（老记录本来也没有命中信息）。
        没有 `cache_hit` 字段的记录（桥重启前写入的）退回固定比例估算。
        """
        p = self.price or dict(FLASH_PRICE_FALLBACK)
        peak = beijing_peak()
        hp = (p["in_hit_peak"] if peak else p["in_hit_off"]) / 1e6
        mp = (p["in_miss_peak"] if peak else p["in_miss_off"]) / 1e6
        out_tok = (p["out_peak"] if peak else p["out_off"]) / 1e6
        ratio = CACHE_HIT_RATIO_DEFAULT
        mixed_in = ratio * hp + (1 - ratio) * mp
        added = 0.0
        try:
            with open(USAGE_LOG, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except Exception:  # noqa: BLE001
                        continue
                    ts = rec.get("ts", 0)
                    if ts <= self.last_ts:
                        continue
                    self.last_ts = ts
                    pin = rec.get("prompt", 0) or 0
                    pout = rec.get("completion", 0) or 0
                    ch = rec.get("cache_hit")
                    if ch is None:
                        # 没有缓存信息（桥重启前写入的记录）→ 退回固定比例估算
                        added += pin * mixed_in + pout * out_tok
                    else:
                        ch = int(ch or 0)
                        cm = int(rec.get("cache_miss") or 0)
                        if ch + cm <= 0:      # 上游给了全 0 的异常数据 → 按全部未命中算
                            cm = pin
                        added += ch * hp + cm * mp + pout * out_tok
        except Exception:  # noqa: BLE001
            pass
        if added:
            self.saved_yuan += added
            self._persist_savings()
        self.l_saved.config(text=f"¥{self.saved_yuan:.2f}")
        self.l_saved_sub.config(text="按官方单价折算的等额费用")

    def _fetch_price_now(self, quiet: bool = False):
        """拉一次价格。quiet=True 用于启动时的静默更新：失败不弹红字、不刷状态栏。"""
        if self._price_fetching:
            return
        self._price_fetching = True

        def work():
            try:
                if not quiet:
                    self.q.put(("status", "正在读取 DeepSeek 官方定价页…"))
                old = dict(self.price or FLASH_PRICE_FALLBACK)
                p, err, source = fetch_flash_prices()
                if p is not None:
                    changed = p != old
                    self.price = p
                    self.price_fetched_at = time.time()
                    self.price_source = source
                    self._persist_savings()
                    self.q.put(("price", None))
                    if changed:
                        # 明确给出“真的变了 + 新数字”，避免“点了却没反应”的错觉
                        self.q.put(("status",
                                    f"价格已更新（来源 {source}）：输出 ¥{p['out_off']:g}/百万(空闲) · "
                                    f"¥{p['out_peak']:g}/百万(高峰)，输入混合 ¥"
                                    f"{self._mixed_input(p, self._measured_hit_ratio()):g}/百万"))
                    else:
                        self.q.put(("status",
                                    f"官方价无变化（来源 {source}）"
                                    "；GUI 统计的是 Flash 列，不是旁边的 v4-pro 列"))
                elif quiet:
                    self.q.put(("price", None))   # 静默失败：只刷新标签，不报错
                else:
                    self.q.put(("price_err", err))
            finally:
                self._price_fetching = False
        threading.Thread(target=work, daemon=True).start()

    def _maybe_auto_update_price(self):
        """启动时若价格已过期（默认 >12h），后台静默拉一次。

        官方价格几个月才动一次，没必要每次开 GUI 都爬，但也不能让它一直停在旧值。
        """
        if time.time() - self.price_fetched_at < PRICE_TTL:
            return
        self._fetch_price_now(quiet=True)

    @staticmethod
    def _mixed_input(p: dict, ratio: float | None = None) -> float:
        """混合输入价：命中占比×命中价 + 未命中占比×未命中价。

        `ratio=None` 时用预设兜底（保持与老调用点 verify_price_parse.py 兼容）。
        """
        hp = p["in_hit_peak"] if beijing_peak() else p["in_hit_off"]
        mp = p["in_miss_peak"] if beijing_peak() else p["in_miss_off"]
        r = CACHE_HIT_RATIO_DEFAULT if ratio is None else ratio
        return r * hp + (1 - r) * mp

    def _on_close(self):
        self._stop = True
        self.destroy()

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    HainnuGUI().mainloop()


