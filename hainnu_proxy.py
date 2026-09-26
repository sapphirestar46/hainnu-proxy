"""
海南师范大学 chat.hainnu.edu.cn (Open WebUI) -> 本地 OpenAI 兼容代理

启动后任何支持 OpenAI API 的客户端都可以这样填：
    Base URL : http://127.0.0.1:8787/v1
    API Key  : 见 config.json 里的 local_api_key（随便填也能通，默认 sk-hainnu）
    Model    : deepseek-chat（自动映射到学校真实模型，或直接填学校模型 id）

原理：学校 Open WebUI 自带 OpenAI 兼容接口 /api/chat/completions，
      但它关闭了 API Key，只认登录后的 JWT。本代理负责带上你的 JWT 转发请求。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import token_codec           # token 落盘加密（Windows DPAPI）：读入后解密

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG: dict[str, Any] = {
    "upstream": "https://chat.hainnu.edu.cn",
    "token_file": "token.txt",
    "port": 8787,
    "host": "127.0.0.1",
    "local_api_key": "sk-hainnu",
    "default_model": "",          # 请求里写了不认识的模型时回退到它
    "aliases": {                  # 客户端常用名 -> 学校真实模型 id
        "deepseek-chat": "",
        "deepseek-reasoner": "",
        "deepseek-v4": "",
        "gpt-4o": "",
        "gpt-4o-mini": "",
    },
    "upstream_retries": 4,      # 上游快速失败时的重试次数（学校后端偶发抽风/限流）
    "rate_limit_cooldown": 12,  # 撞到上游 429 后，全局冷却秒数（让并发请求错峰）
    "timeout": 300,
    "verify_ssl": True,
    "passthrough_unknown_model": False,  # True = 未知模型名直接原样转发给上游
    # ---- 本地自我保护：固定窗口限流 / 并发上限 / 按 Key 配额（默认全关，不影响原有单 Key 行为） ----
    "rate_limit_per_minute": 0,   # >0 = 每个访问 Key 每分钟最多请求数（固定窗口，超出 429+Retry-After）
    "max_concurrency": 0,         # >0 = 同时在途转发请求数上限（信号量），0 = 不限
    "local_keys": [],             # 可选：多 Key + 配额列表，每项 {"key": "...", "daily_tokens":0, "monthly_tokens":0}；0=不限
}

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 读取配置失败({exc})，使用默认配置", file=sys.stderr)
    cfg["aliases"] = {k: v for k, v in (cfg.get("aliases") or {}).items() if v}
    return cfg

CONFIG_PATH = BASE_DIR / "config.json"
CFG = load_config(CONFIG_PATH)


def setup_logger() -> logging.Logger:
    lg = logging.getLogger("hainnu")
    if lg.handlers:
        return lg
    try:
        h = RotatingFileHandler(
            BASE_DIR / "hainnu_proxy.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001
        pass
    return lg

LOG = setup_logger()

# ----------------------------------------------------------------------------
# 令牌用量记录（供 GUI 统计历史流量）
# ----------------------------------------------------------------------------
USAGE_LOG = BASE_DIR / "usage_log.jsonl"


def _cache_from_usage(u: dict) -> tuple[int | None, int | None]:
    """从上游 usage 里取**缓存命中/未命中**的 prompt tokens。

    上游（DeepSeek 原生）同时给两种写法：
      · `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（原生）
      · `prompt_tokens_details.cached_tokens`（OpenAI 风格，只有命中数）
    **上游一个都没给时返回 (None, None)** —— 不能回 0，
    否则会和「确实一条都没命中」混为一谈，把命中率算成假的 0%。
    """
    hit = u.get("prompt_cache_hit_tokens")
    if hit is None:
        det = u.get("prompt_tokens_details")
        if isinstance(det, dict) and "cached_tokens" in det:
            hit = det.get("cached_tokens")
    if hit is None:
        return None, None
    hit = int(hit or 0)
    miss = u.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, int(u.get("prompt_tokens") or 0) - hit)
    return hit, int(miss or 0)


def record_usage_from(data: dict, key: str = "", model: str = "",
                      effort: str = "", ms: float | None = None,
                      stream: bool | None = None,
                      ttft_ms: float | None = None) -> None:
    u = data.get("usage") or {}
    hit, miss = _cache_from_usage(u)
    record_usage(u.get("prompt_tokens"), u.get("completion_tokens"), key=key,
                 cache_hit=hit, cache_miss=miss, model=model, effort=effort,
                 ms=ms, stream=stream, ttft_ms=ttft_ms)


def record_usage(prompt, completion, key: str = "",
                 cache_hit: int | None = None, cache_miss: int | None = None,
                 model: str = "", effort: str = "",
                 ms: float | None = None, stream: bool | None = None,
                 ttft_ms: float | None = None) -> None:
    try:
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        rec = {"ts": time.time(), "prompt": prompt, "completion": completion,
               "total": prompt + completion, "key": key}
        # 缓存命中：**上游给了才写**。老记录没有这两个字段 → 统计时按「无信息」跳过，
        # 不会被当成 0% 命中（详见 _cache_series）。
        if cache_hit is not None or cache_miss is not None:
            rec["cache_hit"] = int(cache_hit or 0)
            rec["cache_miss"] = int(cache_miss or 0)
        if model:
            rec["model"] = str(model)
        if effort:
            rec["effort"] = str(effort)
        if ms is not None:
            rec["ms"] = round(float(ms), 1)
        if ttft_ms is not None:
            rec["ttft_ms"] = round(float(ttft_ms), 1)
        if stream is not None:
            rec["stream"] = bool(stream)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

# ----------------------------------------------------------------------------
# 本地自我保护：固定窗口限流 / 并发上限 / 按 Key 配额 / 用量统计
# ----------------------------------------------------------------------------
_LIMIT_LOCK = threading.Lock()
_LIMIT_CTR: dict[str, list[float]] = {}   # key(id) -> [窗口起点时间, 窗口内计数]
_LIMIT_CALLS = 0

# ----------------------------------------------------------------------------
# 实时“输出速度”：流式转发过程中按内容字符估算输出 token，结束时用服务端
# ----------------------------------------------------------------------------
_OUT_LOCK = threading.Lock()
_OUT = {"started": 0.0, "chars": 0, "tokens": 0, "active": False,
        "last_speed": 0.0, "last_at": 0.0}


def _out_start() -> None:
    with _OUT_LOCK:
        _OUT.update(started=time.time(), chars=0, tokens=0, active=True)


def _out_add(text: str) -> None:
    """流式中按内容长度估算输出 token（中文/英文混合粗略 /3）。"""
    if not text:
        return
    with _OUT_LOCK:
        _OUT["chars"] += len(text)
        _OUT["tokens"] = max(1, int(_OUT["chars"] / 3))


def _out_end(final_tokens: int = 0) -> None:
    """流结束：用服务端 exact completion tokens 校正最终速度并停表。"""
    with _OUT_LOCK:
        if final_tokens > 0:
            _OUT["tokens"] = final_tokens
        el = time.time() - (_OUT["started"] or time.time())
        el = max(el, 0.05)
        _OUT["last_speed"] = _OUT["tokens"] / el
        _OUT["last_at"] = time.time()
        _OUT["active"] = False


def _out_speed() -> tuple[float, bool]:
    """返回 (tok/s, 是否正在输出)。空闲时若超 60s 未输出则视为 0。"""
    with _OUT_LOCK:
        if _OUT["active"]:
            el = max(time.time() - _OUT["started"], 0.05)
            return _OUT["tokens"] / el, True
        if _OUT["last_at"] and (time.time() - _OUT["last_at"]) <= 60:
            return _OUT["last_speed"], False
        return 0.0, False

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


def _is_loopback(req: Request) -> bool:
    """真实对端是否为本机（不看 X-Forwarded-For —— 那个头可伪造）。"""
    return (getattr(req.client, "host", "") or "") in _LOOPBACK_HOSTS


def _client_ip(req: Request) -> str:
    """解析客户端 IP（优先 X-Forwarded-For，回退直连地址），用于限流维度。"""
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return getattr(req.client, "host", "") or "unknown"


def request_key(req: Request) -> str:
    """取请求携带的本地 API Key（Bearer 或 x-api-key）。"""
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        auth = auth[7:]
    k = auth.strip()
    if not k:
        k = (req.headers.get("x-api-key") or "").strip()
    return k


def allowed_keys() -> list[str]:
    ks = [k.get("key") for k in (CFG.get("local_keys") or []) if k.get("key")]
    if ks:
        return ks
    return [CFG.get("local_api_key") or ""]


def rate_limit_allow(key_id: str, per_minute: int) -> tuple[bool, int]:
    """固定窗口限流（内存版，语义同常见的 Redis INCR+PEXPIRE 实现）：
    窗口=1 分钟，首个请求落在窗口内计数；超上限返回 False 与 Retry-After 秒数。"""
    global _LIMIT_CALLS
    if per_minute <= 0:
        return True, 0
    win = per_minute * 60
    now = time.time()
    with _LIMIT_LOCK:
        _LIMIT_CALLS += 1
        if _LIMIT_CALLS % 4096 == 0:     # 偶尔清理过期窗口，防内存无界增长
            cutoff = now - 3600
            for k in [k for k, e in _LIMIT_CTR.items() if e[0] < cutoff]:
                _LIMIT_CTR.pop(k, None)
        e = _LIMIT_CTR.get(key_id)
        if not e or now - e[0] >= win:   # 新窗口：起点=now，计数=1（等价 INCR 后 PEXPIRE）
            _LIMIT_CTR[key_id] = [now, 1]
            return True, 0
        e[1] += 1
        if e[1] <= per_minute:
            return True, 0
        retry = int(win - (now - e[0])) + 1   # 剩余窗口秒数（向上取整），即 Retry-After
        return False, retry

# 并发上限：在途转发请求超过 max_concurrency 时直接 429（快速拒绝，别让它们涌进上游）
if CFG.get("max_concurrency") and int(CFG["max_concurrency"]) > 0:
    CONCURRENCY_SEM = asyncio.Semaphore(int(CFG["max_concurrency"]))
else:
    CONCURRENCY_SEM = None


def quota_state(key_id: str) -> tuple[bool, str]:
    """按 Key 配额校验（读 usage_log 里该 Key 今天/本月的用量，与配置上限比较）。
    返回 (是否放行, 拒绝原因/空串)。未配置该 Key 配额则放行。"""
    keys = CFG.get("local_keys") or []
    if not keys:
        return True, ""
    entry = next((k for k in keys if k.get("key") == key_id), None)
    if not entry:
        return True, ""
    daily = int(entry.get("daily_tokens") or 0)
    monthly = int(entry.get("monthly_tokens") or 0)
    if daily <= 0 and monthly <= 0:
        return True, ""
    # 今天的起点 与 本月的起点
    now = time.time()
    tz = time.localtime().tm_gmtoff
    today0 = (now + tz) // 86400 * 86400 - tz
    lt = time.localtime(now)
    month0 = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    used_d = used_m = 0
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
                if rec.get("key") != key_id:
                    continue
                ts = rec.get("ts", 0)
                if ts < month0:
                    continue
                total = int(rec.get("prompt") or 0) + int(rec.get("completion") or 0)
                used_m += total
                if ts >= today0:
                    used_d += total
    except Exception:  # noqa: BLE001
        pass
    if daily > 0 and used_d > daily:
        return False, f"今日 token 用量 {used_d} 已超该 Key 每日配额 {daily}"
    if monthly > 0 and used_m > monthly:
        return False, f"本月 token 用量 {used_m} 已超该 Key 每月配额 {monthly}"
    return True, ""


def _usage_series(since: float, key: str | None = None) -> tuple[int, int, int]:
    """统计 [since, 现在) 的总输入/输出/请求数（读 usage_log）；可只按某个 Key 过滤。"""
    tin = tout = cnt = 0
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
                if rec.get("ts", 0) < since:
                    continue
                if key and rec.get("key") != key:
                    continue
                tin += int(rec.get("prompt") or 0)
                tout += int(rec.get("completion") or 0)
                cnt += 1
    except Exception:  # noqa: BLE001
        pass
    return tin, tout, cnt


def today_start() -> float:
    """本地时区今天 0 点的绝对时间戳（Git Bash 里 date 不好算，统一用这个）。"""
    now = time.time()
    tz = time.localtime(now).tm_gmtoff
    return (now + tz) // 86400 * 86400 - tz


def _cache_series(since: float, key: str | None = None) -> dict:
    """统计 [since, 现在) 的**缓存命中**：命中/未命中的 prompt tokens、命中的请求数、命中率。

    数据来自 `record_usage()` 落盘的 `cache_hit` / `cache_miss` 字段。
    ⚠️ **加这两个字段之前的老记录没有它们** → 只计入 `unknown_requests`，
    不参与命中率计算；否则历史请求会把命中率拉成一个假数字。
    `rate` 为 None 表示「还没有任何带缓存信息的记录」。
    """
    st = {"hit_tokens": 0, "miss_tokens": 0, "hit_requests": 0,
          "known_requests": 0, "unknown_requests": 0, "requests": 0, "rate": None}
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
                if rec.get("ts", 0) < since:
                    continue
                if key and rec.get("key") != key:
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
        st["rate"] = round(st["hit_tokens"] / tot * 100, 2)
    return st


def get_token() -> str:
    payload = (CFG.get("token") or "").strip()
    if not payload:
        tf = BASE_DIR / CFG["token_file"]
        if tf.exists():
            payload = tf.read_text(encoding="utf-8").strip()
    if not payload:
        raise RuntimeError(
            "没有令牌：请先登录学校站点拿到 JWT 并写入 token.txt，"
            "或运行 python get_token.py 自动获取。"
        )
    # token 落盘是 Windows DPAPI 加密（`enc:` 前缀）；旧版明文也兼容。解密失败=换机/换用户。
    dec = token_codec.decrypt(payload)
    if dec is None:
        raise RuntimeError(
            "令牌为加密格式，但无法在本机当前用户下解密（可能文件被拷到别的电脑/换过用户）。"
            "请重新运行「1.获取令牌.bat」重新登录获取新令牌。"
        )
    return dec.replace("Bearer ", "").strip()


def upstream(path: str) -> str:
    return CFG["upstream"].rstrip("/") + path

# ----------------------------------------------------------------------------
# 模型目录
# ----------------------------------------------------------------------------
MODEL_CACHE: dict[str, Any] = {"ids": [], "at": 0.0, "raw": None}


def oai_model_list(ids: list[str]) -> dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "hainnu"}
            for m in ids
        ],
    }


def fetch_models(force: bool = False) -> list[str]:
    """从学校 Open WebUI 拉取可用模型 id。失败则保留上次结果。"""
    if not force and MODEL_CACHE["ids"] and time.time() - MODEL_CACHE["at"] < 300:
        return MODEL_CACHE["ids"]
    try:
        with httpx.Client(
            timeout=20, verify=CFG["verify_ssl"], follow_redirects=True,
            trust_env=CFG.get("respect_proxy_env", False),
        ) as cli:
            r = cli.get(
                upstream("/api/models"),
                headers={
                    "Authorization": f"Bearer {get_token()}",
                    "Accept": "application/json",
                },
            )
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            ids: list[str] = []
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("id") or it.get("name") or it.get("model")
                    if mid:
                        ids.append(str(mid))
                elif isinstance(it, str):
                    ids.append(it)
            if ids:
                MODEL_CACHE.update({"ids": ids, "at": time.time(), "raw": data})
                print(f"[info] 拉取到 {len(ids)} 个模型: {ids}", flush=True)
                return ids
        print(
            f"[warn] 拉取模型失败 HTTP {r.status_code}: {r.text[:200]}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 拉取模型异常: {exc}", file=sys.stderr, flush=True)
    if not MODEL_CACHE["ids"] and CFG.get("static_models"):
        MODEL_CACHE["ids"] = list(CFG["static_models"])
    return MODEL_CACHE["ids"]

_WARNED_ROUTES: set[tuple[str, str]] = set()

# 模型名分词：任何非字母数字都当分隔符
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _model_tokens(name: str) -> set[str]:
    """把模型名切成小写词元，用于纯名字相似度比较。

    "deepseek-ai/DeepSeek-V4-Flash" -> {"deepseek", "ai", "v4", "flash"}
    """
    return {t for t in _TOKEN_SPLIT.split((name or "").lower()) if t}


def _pick_by_name(req_model: str, ids: list[str]) -> str:
    """在上游模型列表里按「名字像不像」挑一个，挑不出返回 ""。

    设计目标：**不查任何写死的映射表**。学校换模型名时（实测
    `deepseek-ai/DeepSeek-V4-Flash` → `deepseek-flash`），客户端往往还写着旧名或
    通用名（`deepseek-chat`）；这里仅靠词元重合度就能把请求接到新名字上。

    打分：覆盖率（请求词有多少落在候选名里）> 交集大小 > 名字更短的优先。
    """
    if not ids:
        return ""
    want = _model_tokens(req_model)
    if not want:
        return ""
    best_key: tuple[float, int, int] | None = None
    best_id = ""
    for m in ids:
        inter = want & _model_tokens(m)
        if not inter:
            continue
        key = (len(inter) / len(want), len(inter), -len(m))
        if best_key is None or key > best_key:
            best_key, best_id = key, m
    return best_id


def _effective_model(prefer: str, ids: list[str]) -> str:
    """校验一个模型名是否仍在上游列表里；不在就退到上游第一个可用模型。

    `ids` 为空（没拉到列表）时不判断，避免网络抖动把好配置误判死。
    """
    if not prefer or not ids or prefer in ids:
        return prefer
    fallback = _pick_by_name(prefer, ids) or ids[0]
    print(
        f"[warn] 模型 {prefer!r} 已不在上游列表 {ids} 中（学校可能换了模型 id），"
        f"自动改走 {fallback!r}",
        file=sys.stderr,
        flush=True,
    )
    return fallback


def resolve_model(req_model: str) -> str:
    """把客户端写的模型名落到上游真实存在的 id 上。

    优先级：别名表（用户显式配置，可留空）> 精确命中 > 去 `:tag` 后命中 >
    名字相似度 > passthrough > default_model（可留空）> 上游第一个。
    全程以上游 `/api/models` 的实时结果为准，不依赖任何写死的模型名。
    """
    ids = fetch_models()
    aliases: dict[str, str] = CFG.get("aliases") or {}
    default = (CFG.get("default_model") or "").strip()

    # 仅当上游只有一个模型时才敢替用户兜底；多个模型时不擅自替人做选择
    sole = ids[0] if len(ids) == 1 else ""

    if req_model and req_model in aliases:
        return _effective_model(aliases[req_model], ids)

    if not req_model:
        return _effective_model(default, ids) or sole or (ids[0] if ids else "")

    # 精确命中
    if req_model in ids:
        return req_model

    # 忽略 ":latest" 之类后缀后命中
    stripped = re.sub(r":[A-Za-z0-9_.\-]+$", "", req_model)
    for m in ids:
        if m == stripped or re.sub(r":[A-Za-z0-9_.\-]+$", "", m) == stripped:
            return m

    # 纯名字相似度（不依赖任何映射表）
    picked = _pick_by_name(req_model, ids)
    if picked:
        return picked

    if CFG.get("passthrough_unknown_model"):
        return req_model

    fallback = _effective_model(default, ids) or sole
    if not fallback and ids:
        fallback = ids[0]
    if fallback:
        if (req_model, fallback) not in _WARNED_ROUTES:
            _WARNED_ROUTES.add((req_model, fallback))
            print(
                f"[warn] 请求的模型 {req_model!r} 在上游列表 {ids} 里找不到对应项，"
                f"已按默认路由到 {fallback!r}",
                file=sys.stderr,
                flush=True,
            )
        return fallback

    # 上游列表也拉不到：原样转发，让上游自己报错，别静默改成别的东西
    return req_model

# ----------------------------------------------------------------------------
# 请求归一化
# ----------------------------------------------------------------------------

def normalize_request(body: dict) -> dict:
    """把各家客户端的写法翻译成上游认得的 OpenAI 形状。

    主要处理 DSH / pi-ai 这类客户端的两个已知差异：
      1. 系统提示用 role="developer" 发送 → 上游只认 "system"
      2. 输出上限写在 max_completion_tokens → 上游只认 max_tokens
    """
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "developer":
            m["role"] = "system"
            LOG.info("normalized: developer -> system")

    if "max_completion_tokens" in body:
        val = body.pop("max_completion_tokens")
        if "max_tokens" not in body or not body.get("max_tokens"):
            body["max_tokens"] = val
            LOG.info("normalized: max_completion_tokens -> max_tokens = %s", val)

    return body

EFFORT_VALUES = ("low", "medium", "high", "minimal", "auto")
EFFORT_OFF = ("none", "off", "disabled", "no", "false", "0")


def reasoning_override(req: Request) -> str:
    """从请求头 / 查询参数取思考强度覆盖值；返回 '' 表示客户端没有表态。"""
    raw = req.headers.get("x-reasoning-effort") or req.query_params.get("reasoning_effort") or ""
    return raw.strip().lower()


def apply_reasoning_effort(req: Request, body: dict) -> None:
    """按上面的三层优先级决定最终 reasoning_effort（就地在 body 上生效）。"""
    eff = reasoning_override(req)
    skip_global = False
    if eff and "reasoning_effort" not in body:
        if eff in EFFORT_OFF:
            skip_global = True          # 客户端明确要关思考：连 config 兜底也别注入
            LOG.info("reasoning_effort: disabled by client override")
        elif eff in EFFORT_VALUES:
            body["reasoning_effort"] = eff
            LOG.info("reasoning_effort: %s (client override)", eff)
        else:
            LOG.warning("reasoning_effort: 忽略无法识别的覆盖值 %r", eff)
    for k, v in (CFG.get("extra_body") or {}).items():
        if skip_global and k == "reasoning_effort":
            continue
        body.setdefault(k, v)           # 客户端已给值的不覆盖

# ----------------------------------------------------------------------------
# 鉴权 / 通用转发
# ----------------------------------------------------------------------------


def auth_ok(req: Request) -> bool:
    presented = request_key(req)
    ks = allowed_keys()
    if CFG.get("local_keys"):
        # 配置了多 Key：只认在 list 里的 Key
        return bool(ks) and presented in ks
    key = (CFG.get("local_api_key") or "").strip()
    if not key:
        return True
    return presented == key


def fwd_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def client() -> httpx.Client:
    # respect_proxy_env 默认 False：直连学校，不经过本机本地代理
    # （学校站点应直连；走本地代理常触发 Open WebUI 的 402 Server Connection Error）
    return httpx.Client(
        timeout=CFG["timeout"], verify=CFG["verify_ssl"], follow_redirects=True,
        trust_env=CFG.get("respect_proxy_env", False),
    )

# ----------------------------------------------------------------------------
# FastAPI
# ----------------------------------------------------------------------------
app = FastAPI(title="hainnu-proxy")


def probe_upstream() -> dict:
    """发一个最小请求，确认学校后端是否真的能出字（而不是只列出模型）。"""
    body = {
        "model": resolve_model(""),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }
    t0 = time.time()
    try:
        with client() as cli:
            r = cli.post(upstream("/api/chat/completions"), headers=fwd_headers(), json=body)
        dt = round(time.time() - t0, 2)
        if r.status_code == 200:
            try:
                c = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
            except Exception:  # noqa: BLE001
                c = None
            return {"ok": True, "status": 200, "content": (c or "")[:80], "elapsed": dt}
        out = {"ok": False, "status": r.status_code, "detail": r.text[:200], "elapsed": dt}
        if "Server Connection Error" in r.text or r.status_code in (402, 500, 502, 503):
            out["hint"] = (
                "学校 Open WebUI 的上游模型服务返回 402（Payment Required，额度/计费问题）。"
                "本地代理、网络、代码均正常；浏览器此刻发消息也会报同样的错。"
                "请联系学校信息中心/网络中心处理；学校修好后本地无需任何改动即可恢复。"
            )
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

# ----------------------------------------------------------------------------
# 上游限流（429）识别与退避
# ----------------------------------------------------------------------------
_WRAPPED_STATUS_RE = re.compile(r'"detail"\s*:\s*"(?P<code>\d{3})\s*:\s*Open WebUI')
_RETRYABLE_CODES = (402, 408, 429)

_RATE_COOLDOWN = {  # level: 连续限流冷却升级档
    "until": 0.0, "last_ts": 0.0, "level": 0,
    # ↓ 只用于「记录/统计」，不参与任何判断逻辑
    "hits": 0, "first_ts": 0.0, "last_body": "", "last_status": 0, "last_where": "",
}

PROXY_START = time.time()   # 进程启动时刻，供 /health 计算“持续运行时间”


def wrapped_status(text: str) -> int | None:
    """从 Open WebUI 的 400 包装错误里解出真实状态码；不是这种格式返回 None。"""
    if not text:
        return None
    m = _WRAPPED_STATUS_RE.search(text[:300])
    if not m:
        return None
    try:
        return int(m.group("code"))
    except (TypeError, ValueError):
        return None


def is_rate_limited(status: int, text: str = "") -> bool:
    """上游限流/配额类错误（含被包装成 HTTP 400 的 429）。"""
    if status in _RETRYABLE_CODES or status >= 500:
        return True
    w = wrapped_status(text)
    return w is not None and (w in _RETRYABLE_CODES or w >= 500)


def note_rate_limited(status: int, text: str, where: str = "") -> None:
    """只有真 429 才打全局冷却；5xx 属于上游抽风，快速重试即可，不该拖慢。
    连续撞 429 会按 90 秒内再次触发自动升级冷却时长（×2 递增），最多到基数×32，
    让学校背后的模型供应商（按 RPM/TPM 限流）有足够时间放行，而不是紧跟着又打爆。

    注：这套梯度重试是刻意设计的自动化行为，不要"优化"成线性封顶之类 ——
    曾经改过一次（按"学校一般 1 分钟内放行"封顶到 75s），被明确要求还原。
    """
    if status == 429 or wrapped_status(text) == 429:
        via = "HTTP 429" if status == 429 else "包装400内429"
        now = time.time()
        R = _RATE_COOLDOWN
        R["hits"] = int(R.get("hits", 0)) + 1
        if not R.get("first_ts"):
            R["first_ts"] = now
        R["last_status"] = status
        R["last_body"] = (text or "").strip().replace("\n", " ")[:300]
        R["last_where"] = where or "-"
        LOG.warning(
            "限流命中 #%d（%s @ %s）：原始状态=%s，上游原文=%s；本轮首次命中于 %s，距首次 %.0fs",
            R["hits"], via, R["last_where"], status,
            (R["last_body"][:180] or "(空)"),
            time.strftime("%H:%M:%S", time.localtime(R["first_ts"])),
            now - R["first_ts"],
        )
        since = now - _RATE_COOLDOWN["last_ts"]
        # ⚠️ 谨慎改进（梯度本身保留不动，只修一个明显缺陷）：
        waited_out = _RATE_COOLDOWN["until"] > 0 and now >= _RATE_COOLDOWN["until"]
        if _RATE_COOLDOWN["last_ts"] > 0 and since < 90 and waited_out:
            level = min(_RATE_COOLDOWN["level"] + 1, 5)   # 等完一轮还撞 → 再升一档
        elif since >= 180:
            level = 0                                     # 久未触发 → 冷却档位回落
        else:
            level = _RATE_COOLDOWN["level"]
        _RATE_COOLDOWN["level"] = level
        _RATE_COOLDOWN["last_ts"] = now
        base = float(CFG.get("rate_limit_cooldown", 12))
        wait = base * (2 ** level) + random.uniform(0, 6)
        until = now + wait
        if until > _RATE_COOLDOWN["until"]:
            _RATE_COOLDOWN["until"] = until
            LOG.warning("识别到限流（%s），全局冷却 %.1fs（档%d），后续请求错峰发出", via, wait, level)


def wait_rate_cooldown() -> None:
    """若正处于限流冷却期，先等过去再发请求（带抖动，避免同时放行再撞一次）。

    冷却「刚好走完」时会记一条解除日志，这样从日志就能算出这次停用总共持续了多久、
    期间撞了多少次 —— 便于统计学校的限流行为。
    """
    delay = _RATE_COOLDOWN["until"] - time.time()
    if delay <= 0 and _RATE_COOLDOWN["until"] > 0:
        _first = _RATE_COOLDOWN.get("first_ts") or 0.0
        LOG.warning("限流冷却结束，恢复放行（本轮命中 %d 次，自首次命中起 %.0fs）",
                    _RATE_COOLDOWN.get("hits", 0),
                    max(0.0, time.time() - _first) if _first else 0.0)
        _RATE_COOLDOWN["until"] = 0.0        # 本来就已经过期，置 0 只为标记「本轮结束」
        _RATE_COOLDOWN["hits"] = 0
        _RATE_COOLDOWN["first_ts"] = 0.0
        return
    if delay > 0:
        LOG.warning("限流冷却等待 %.1fs（%s 放行）",
                    delay,
                    time.strftime("%H:%M:%S", time.localtime(_RATE_COOLDOWN["until"])))
        time.sleep(delay + random.uniform(0, 1.5))


def backoff(attempt: int) -> float:
    """指数退避 + 抖动，避免多个并发请求同时重试又撞在一起。总时长严格上限 30s（含抖动）。"""
    d = min(1.5 * (2 ** attempt), 30.0)
    wait = min(d + random.uniform(0, 0.8), 30.0)
    LOG.warning("退避重试 attempt=%d delay=%.2fs", attempt + 1, wait)
    return wait


def remap_model_after_not_found(payload: dict, text: str, req_model: str = "") -> bool:
    """上游回 "Model not found" 时自愈：刷新模型列表 → 重新解析 → 允许重试一轮。

    学校换模型 id 后，桥最多还会拿着 5 分钟内拉到的旧列表；这一层让改名生效后的
    第一个请求自己接上，不用人工改配置、也不用重启代理。
    优先拿客户端原始名字对新列表再解析一次，解析不出才退回默认/上游首个。
    """
    if "model not found" not in (text or "").lower():
        return False
    fresh = fetch_models(force=True)
    if not fresh:
        return False
    new = resolve_model(req_model) if req_model else ""
    if not new or new == payload.get("model"):
        new = resolve_model("")
    if new and new != payload.get("model"):
        LOG.warning(
            "模型 %r 上游已不存在，刷新后列表为 %s，自动改走 %r",
            payload.get("model"), fresh, new,
        )
        payload["model"] = new
        return True
    return False


@app.get("/health")
async def health(request: Request, probe: int = 0):
    try:
        get_token()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if probe and not _is_loopback(request):
        probe = 0
    info = {
        "ok": True,
        "upstream": CFG["upstream"],
        "models": fetch_models(),
        "default_model": resolve_model(""),
        "aliases": CFG.get("aliases"),
    }
    # 限流状态（供 GUI 显示自动冷却/重试是否在保护）
    _now = time.time()
    info["rate_limited_now"] = _now < _RATE_COOLDOWN["until"]
    info["rate_cooldown_rem"] = max(0.0, round(_RATE_COOLDOWN["until"] - _now, 1))
    info["rate_until_ts"] = _RATE_COOLDOWN["until"]     # 绝对时间戳，GUI 逐秒倒数
    info["rate_last_ts"] = _RATE_COOLDOWN["last_ts"]
    # 限流统计（供 GUI / 外部脚本直接读，便于统计）
    info["rate_hits"] = _RATE_COOLDOWN.get("hits", 0)          # 本轮已命中次数
    info["rate_first_ts"] = _RATE_COOLDOWN.get("first_ts", 0.0)  # 本轮首次命中时刻
    info["rate_last_where"] = _RATE_COOLDOWN.get("last_where", "")  # 哪条链路撞的
    info["rate_last_body"] = _RATE_COOLDOWN.get("last_body", "")    # 上游原文（截断）
    info["rate_cooldown_secs"] = CFG.get("rate_limit_cooldown", 12)
    # 缓存命中（prompt cache）：今日 + 全量。rate=None 表示还没有带缓存信息的记录。
    info["cache_today"] = _cache_series(today_start())
    info["cache_all"] = _cache_series(0.0)
    info["uptime"] = time.time() - PROXY_START         # 代理持续运行秒数
    # 实时/最近输出速度（tok/s，仅计输出，供 GUI 显示贴近 harness 的读数）
    _spd, _act = _out_speed()
    info["output_speed"] = round(_spd, 1)
    info["output_active"] = _act
    # /health?probe=1 会真的打一次上游，用来判断学校后端是否可用
    if probe:
        info["upstream_chat"] = probe_upstream()
        info["ok"] = bool(info["upstream_chat"].get("ok"))
    return info


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": {"message": "bad local api key"}}, status_code=401)
    ids = fetch_models(force=True)
    if not ids:
        return JSONResponse(
            {"error": {"message": "取不到模型列表，检查令牌是否有效", "type": "upstream_error"}},
            status_code=502,
        )
    return oai_model_list(ids)


@app.get("/v1/usage")
@app.get("/usage")
async def usage(request: Request):
    """用量可见：今天/本月的输入·输出·请求数，按 Key 时可分账。"""
    if not auth_ok(request):
        return JSONResponse({"error": {"message": "bad local api key"}}, status_code=401)
    now = time.time()
    today0 = today_start()
    lt = time.localtime(now)
    month0 = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))

    def agg(since, key=None):
        tin, tout, cnt = _usage_series(since, key)
        c = _cache_series(since, key)
        return {"input": tin, "output": tout, "total": tin + tout, "requests": cnt,
                # 缓存命中：命中/未命中的 prompt tokens、命中率(%)、命中的请求数
                "cache_hit_tokens": c["hit_tokens"],
                "cache_miss_tokens": c["miss_tokens"],
                "cache_hit_rate": c["rate"],
                "cache_hit_requests": c["hit_requests"],
                "cache_known_requests": c["known_requests"]}

    res = {
        "rate_limit_per_minute": int(CFG.get("rate_limit_per_minute") or 0),
        "max_concurrency": int(CFG.get("max_concurrency") or 0),
        "today": agg(today0),
        "month": agg(month0),
        "keys": {},
    }
    for k in allowed_keys():
        if k:
            res["keys"][k] = {
                "today": agg(today0, k),
                "month": agg(month0, k),
            }
    return res


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):

    # 本地鉴权后先做 固定窗口限流 / 按 Key 配额 / 并发保护，
    # 全部默认关闭；命中返回 429（限流带 Retry-After 头）。
    if not auth_ok(request):
        return JSONResponse({"error": {"message": "bad local api key"}}, status_code=401)
    key_id = request_key(request)
    per = int(CFG.get("rate_limit_per_minute") or 0)
    ok, retry = rate_limit_allow(key_id + "|" + _client_ip(request), per)
    if not ok:
        return JSONResponse(
            {"error": {"message": "本地限流：请求过于频繁，请稍后重试",
                       "type": "rate_limited"}},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    okq, msg = quota_state(key_id)
    if not okq:
        return JSONResponse({"error": {"message": msg, "type": "quota_exceeded"}},
                            status_code=429)

    if CONCURRENCY_SEM is not None:
        if CONCURRENCY_SEM.locked():
            return JSONResponse(
                {"error": {"message": "本地代理当前并发已满，请稍后重试", "type": "overloaded"}},
                status_code=429,
            )
        async with CONCURRENCY_SEM:
            return await _chat_impl(request, key_id)
    return await _chat_impl(request, key_id)


async def _chat_impl(request: Request, key_id: str):
    if not auth_ok(request):
        return JSONResponse({"error": {"message": "bad local api key"}}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": {"message": "invalid json body"}}, status_code=400)

    # 调试捕获：记录客户端（如 DSH）发来的原始请求体，用于复现疑难问题
    if CFG.get("debug_capture"):
        try:
            with open(BASE_DIR / "debug_capture.log", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%m-%d %H:%M:%S"), "dir": "request",
                                    "body": body}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # 客户端原始写的模型名（可能已过期）。下面 resolve_model 会把它落到上游真名上；
    # 若上游仍回 "Model not found"，用这个名字对着刷新后的列表再解析一次（见 remap 自愈）。
    _req_model = str(body.get("model") or "")
    if "model" in body:
        body["model"] = resolve_model(_req_model)
    else:
        body["model"] = resolve_model("")
    body = normalize_request(body)
    # 思考强度：客户端显式值 > 客户端请求头 > config 的 extra_body（见函数上方注释）
    apply_reasoning_effort(request, body)
    want_stream = bool(body.get("stream"))
    _t0 = time.time()
    LOG.info(
        "openai chat model=%s stream=%s msgs=%d effort=%s",
        body.get("model"), want_stream, len(body.get("messages") or []),
        body.get("reasoning_effort") or "-",
    )

    url = upstream("/api/chat/completions")
    headers = fwd_headers()

    def on_upstream_error(status: int, text: str):
        LOG.warning("upstream %s (%d): %s", "chat", status, text[:200])
        if status in (401, 403):
            return JSONResponse(
                {
                    "error": {
                        "message": "学校的登录令牌已失效，请重新登录后更新 token.txt",
                        "type": "auth_expired",
                        "upstream_status": status,
                        "upstream_body": text[:500],
                    }
                },
                status_code=status,
            )
        if "Server Connection Error" in text or status in (402, 500, 502, 503):
            if status == 402 or "402" in text:
                message = (
                    "学校 Open WebUI 的上游模型服务返回 402（Payment Required，额度/计费问题）。"
                    "本地代理与网络均正常，浏览器此刻发消息也会报同样的错。"
                    "请联系学校信息中心/网络中心处理；学校修好后本地无需任何改动即可恢复，"
                    "可运行 7.恢复监测.bat 持续探测。"
                )
            else:
                message = (
                    "学校后端暂时不可用（Open WebUI 连不上模型服务），本地代理本身是好的。"
                    "请稍后重试；可访问 http://127.0.0.1:8787/health?probe=1 查看是否已恢复。"
                )
        else:
            message = text[:800]
        return JSONResponse(
            {
                "error": {
                    "message": message,
                    "type": "upstream_unavailable" if "Server Connection Error" in text else "upstream_error",
                    "upstream_status": status,
                    "upstream_body": text[:500],
                }
            },
            status_code=status,
        )

    attempts = max(1, int(CFG.get("upstream_retries", 4)))

    if not want_stream:
        r = None
        for i in range(attempts):
            wait_rate_cooldown()
            with client() as cli:
                r = cli.post(url, headers=headers, json=body)
            if r.status_code == 200:
                break
            LOG.warning(
                "upstream chat %d (attempt %d/%d): %s",
                r.status_code, i + 1, attempts, r.text[:200],
            )
            note_rate_limited(r.status_code, r.text, "openai")
            if i < attempts - 1 and remap_model_after_not_found(body, r.text, _req_model):
                continue
            if i < attempts - 1 and is_rate_limited(r.status_code, r.text):
                time.sleep(backoff(i))
                continue
            return on_upstream_error(r.status_code, r.text)
        if r is None or r.status_code != 200:
            return on_upstream_error(getattr(r, "status_code", 502), getattr(r, "text", ""))
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                {"error": {"message": "上游返回的不是 JSON", "body": r.text[:800]}},
                status_code=502,
            )
        record_usage_from(data, key_id, model=str(body.get("model") or ""),
                          effort=str(body.get("reasoning_effort") or ""),
                          ms=(time.time() - _t0) * 1000, stream=False)
        LOG.info("openai chat ok %.1fs", time.time() - _t0)
        return JSONResponse(data)

    dbg_on = bool(CFG.get("debug_capture"))
    raw_events: list[bytes] = []
    last_usage: dict[str, Any] = {}  # 最后一次带 usage 的事件（流读完统一记一次，避免逐 chunk 重复）
    reasoning_buf: list[str] = []
    emitted = {"content": False}
    ttft_ms = {"v": None}

    def _mark_ttft() -> None:
        if ttft_ms["v"] is None:
            ttft_ms["v"] = (time.time() - _t0) * 1000

    def process_event(ev: bytes) -> bytes:
        """观察单个 SSE 事件：记用量、是否已出正文、输出速度。不改写事件。"""
        if not ev.startswith(b"data:"):
            return ev
        payload = ev[5:].strip()
        if payload == b"[DONE]" or not payload:
            return ev
        try:
            obj = json.loads(payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return ev
        if obj.get("usage"):
            last_usage.clear()
            last_usage.update(obj)
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            rc = d.get("reasoning_content")
            if isinstance(rc, str) and rc:
                reasoning_buf.append(rc)
                _mark_ttft()
            c = d.get("content")
            if isinstance(c, str) and c:
                emitted["content"] = True
                _out_add(c)   # 实时输出速度：按内容估算
                _mark_ttft()
            if d.get("tool_calls"):
                emitted["content"] = True
                _mark_ttft()
        return ev

    done_sent = {"v": False}

    cli = client()
    r = None
    for i in range(attempts):
        wait_rate_cooldown()
        try:
            r = cli.send(cli.build_request("POST", url, headers=headers, json=body), stream=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("upstream connect failed (attempt %d/%d): %s", i + 1, attempts, exc)
            r = None
            if i < attempts - 1:
                time.sleep(1.0 * (i + 1))
                continue
            cli.close()
            return JSONResponse(
                {"error": {"message": f"上游连接失败: {exc}", "type": "upstream_error"}},
                status_code=502,
            )
        if r.status_code == 200:
            break
        text = r.read().decode("utf-8", "replace")
        r.close()
        LOG.warning(
            "upstream stream %d (attempt %d/%d): %s",
            r.status_code, i + 1, attempts, text[:200],
        )
        note_rate_limited(r.status_code, text, "openai")
        if i < attempts - 1 and remap_model_after_not_found(body, text, _req_model):
            continue
        if i < attempts - 1 and is_rate_limited(r.status_code, text):
            time.sleep(backoff(i))
            continue
        cli.close()
        return on_upstream_error(r.status_code, text)

    if r is None or r.status_code != 200:
        cli.close()
        return on_upstream_error(getattr(r, "status_code", 502), getattr(r, "text", ""))

    # 上游已确认 200，开始流式转发 -> 计时输出速度
    _out_start()

    def emit(ev: bytes):
        """产出单个事件。"""
        if dbg_on:
            raw_events.append(ev)
        stripped = ev.strip()
        if stripped.startswith(b"data:") and stripped[5:].strip() == b"[DONE]":
            done_sent["v"] = True
            yield stripped + b"\n\n"
            return
        out = process_event(ev)
        if out:
            yield out + b"\n\n" if not out.endswith(b"\n\n") else out

    # 流的状态。放在 gen() 外面，好让收尾函数也能读到（嵌套函数只能闭包外层的变量）。
    stream_state = {"finished": False, "aborted": False}

    def finalize_stream() -> None:
        """流收尾：统计用量、关上游连接、写日志。

        必须无条件执行 —— 客户端中途取消时也要跑到，所以只放在 finally 里，
        并且这里**绝不 yield**（见 gen() 里 GeneratorExit 分支的说明）。
        """
        try:
            if last_usage:
                record_usage_from(last_usage, model=str(body.get("model") or ""),
                                  effort=str(body.get("reasoning_effort") or ""),
                                  ms=(time.time() - _t0) * 1000, stream=True,
                                  ttft_ms=ttft_ms["v"])
                _out_end((last_usage.get("usage") or {}).get("completion_tokens") or 0)
            else:
                _out_end()
        except Exception:  # noqa: BLE001
            LOG.exception("finalize: 用量统计失败")
        if dbg_on:
            # 调试捕获：每次流结束记一行摘要；若全程无正文，附带全部原始事件
            try:
                with open(BASE_DIR / "debug_capture.log", "a", encoding="utf-8") as f:
                    rec = {
                        "ts": time.strftime("%m-%d %H:%M:%S"), "dir": "stream",
                        "content_emitted": emitted["content"],
                        "reasoning_len": sum(map(len, reasoning_buf)),
                        "n_events": len(raw_events),
                        "finished": stream_state["finished"],
                        "aborted": stream_state["aborted"],
                    }
                    if not emitted["content"]:
                        rec["events"] = [e.decode("utf-8", "replace") for e in raw_events]
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass
        LOG.info(
            "openai stream done %.1fs finished=%s aborted=%s",
            time.time() - _t0, stream_state["finished"], stream_state["aborted"],
        )

    def gen():
        try:
            buf = b""
            for chunk in r.iter_bytes():
                if not chunk:
                    continue
                buf += chunk
                while b"\n\n" in buf:
                    ev, buf = buf.split(b"\n\n", 1)
                    yield from emit(ev)
            if buf:
                yield from emit(buf.rstrip(b"\r\n"))
            stream_state["finished"] = True
        except GeneratorExit:
            stream_state["aborted"] = True
            LOG.info("client disconnected, closing upstream stream early")
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.exception("openai stream failed mid-stream: %s", exc)
            if not emitted["content"] and not reasoning_buf:
                # 一条内容都没出：回报真实错误，避免“completed response with no content”。
                # 若属限流前的连接被断，客户端看到的是明确错误而非空成功。
                msg = json.dumps({"error": {"message": f"上游连接失败: {exc}"}})
                yield ("data: " + msg + "\n\n").encode()
            else:
                # 已出部分内容：不注入 error，走下面的收尾补 [DONE]，
                # 客户端得到“已收到的内容 + 干净收尾”，会话不被限流中途掐断。
                LOG.warning("upstream stream teardown 后已出内容，优雅收尾（不中断会话）")
            stream_state["finished"] = True
        finally:
            finalize_stream()

        # 正常读完 / 中途出错：补 [DONE]。
        # 客户端提前断开时走不到这里 —— 那种情况由上面的 GeneratorExit 分支处理。
        if stream_state["finished"]:
            if not done_sent["v"]:
                yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/_/reload")
async def reload(request: Request):
    if not auth_ok(request):
        return JSONResponse({"error": {"message": "bad local api key"}}, status_code=401)
    global CFG
    CFG = load_config(CONFIG_PATH)
    ids = fetch_models(force=True)
    return {"ok": True, "models": ids}


def _upstream_sse_events(body: dict, req_model: str = ""):
    """按 SSE 事件边界（\\n\\n）产出上游返回的每个事件。失败时按 OpenAI 路径同样重试。"""
    url = upstream("/api/chat/completions")
    attempts = max(1, int(CFG.get("upstream_retries", 4)))
    last_err = b""
    for i in range(attempts):
        wait_rate_cooldown()
        try:
            with client() as cli:
                with cli.stream("POST", url, headers=fwd_headers(), json=body) as r:
                    status = r.status_code
                    if status == 200:
                        buf = b""
                        for chunk in r.iter_bytes():
                            if not chunk:
                                continue
                            buf += chunk
                            while b"\n\n" in buf:
                                ev, buf = buf.split(b"\n\n", 1)
                                yield ev
                        if buf.strip():
                            yield buf.strip()
                        return
                    text = r.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("upstream anthropic connect failed (attempt %d/%d): %s", i + 1, attempts, exc)
            if i < attempts - 1:
                time.sleep(1.0 * (i + 1))
                continue
            yield b"__ERROR__502:" + str(exc).encode("utf-8", "replace")[:800]
            return
        LOG.warning(
            "upstream anthropic stream %d (attempt %d/%d): %s",
            status, i + 1, attempts, text[:200],
        )
        note_rate_limited(status, text, "anthropic")
        last_err = b"__ERROR__" + str(status).encode() + b":" + text.encode("utf-8", "replace")[:800]
        if i < attempts - 1 and remap_model_after_not_found(body, text, req_model):
            continue
        if i < attempts - 1 and is_rate_limited(status, text):
            time.sleep(backoff(i))
            continue
        yield last_err
        return
    if last_err:
        yield last_err

# ----------------------------------------------------------------------------
# Anthropic Messages API 兼容层
# ----------------------------------------------------------------------------
try:
    import anthropic_compat
except Exception:  # noqa: BLE001  # pragma: no cover
    anthropic_compat = None


def _anth_error(msg: str, status: int = 502):
    return JSONResponse(
        {"type": "error", "error": {"type": "api_error", "message": msg}}, status_code=status
    )


@app.post("/v1/messages")
@app.post("/messages")
async def anthropic_messages(request: Request):
    if anthropic_compat is None:
        return _anth_error("anthropic_compat 模块缺失")
    if not auth_ok(request):
        return _anth_error("bad local api key", 401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _anth_error("invalid json body", 400)

    try:
        oai_body = anthropic_compat.to_openai_request(body, resolve_model)
    except Exception as exc:  # noqa: BLE001
        return _anth_error(f"请求转换失败: {exc}", 400)

    oai_body = normalize_request(oai_body)
    # 思考强度：与 OpenAI 路径同一套优先级（见 apply_reasoning_effort）
    apply_reasoning_effort(request, oai_body)

    model_name = oai_body.get("model", "")
    req_model = str(body.get("model") or "")
    emit_thinking = bool(CFG.get("anthropic_emit_thinking", False))
    attempts = max(1, int(CFG.get("upstream_retries", 4)))
    _t0 = time.time()
    LOG.info(
        "anthropic messages model=%s stream=%s tools=%d effort=%s",
        model_name, bool(oai_body.get("stream")), len(oai_body.get("tools") or []),
        oai_body.get("reasoning_effort") or "-",
    )

    if not oai_body.get("stream"):
        r = None
        for i in range(attempts):
            wait_rate_cooldown()
            with client() as cli:
                r = cli.post(upstream("/api/chat/completions"), headers=fwd_headers(), json=oai_body)
            if r.status_code == 200:
                break
            LOG.warning(
                "upstream anthropic %d (attempt %d/%d): %s",
                r.status_code, i + 1, attempts, r.text[:200],
            )
            note_rate_limited(r.status_code, r.text, "anthropic")
            if i < attempts - 1 and remap_model_after_not_found(oai_body, r.text, req_model):
                continue
            if i < attempts - 1 and is_rate_limited(r.status_code, r.text):
                time.sleep(backoff(i))
                continue
            if r.status_code in (401, 403):
                return _anth_error("学校登录令牌已失效，请重新运行 1.获取令牌.bat", r.status_code)
            return _anth_error(r.text[:400], r.status_code)
        if r is None or r.status_code != 200:
            status = getattr(r, "status_code", 502)
            text = getattr(r, "text", "")
            if status in (401, 403):
                return _anth_error("学校登录令牌已失效，请重新运行 1.获取令牌.bat", status)
            return _anth_error((text or "")[:400], status)
        model_name = str(oai_body.get("model") or model_name)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return _anth_error("上游返回的不是 JSON", 502)
        record_usage_from(data, model=str(model_name or ""),
                          effort=str(oai_body.get("reasoning_effort") or ""),
                          ms=(time.time() - _t0) * 1000, stream=False)
        LOG.info("anthropic messages ok %.1fs", time.time() - _t0)
        return JSONResponse(
            anthropic_compat.to_anthropic_response(data, model_name, emit_thinking)
        )

    # ---- 流式 ----
    streamer = anthropic_compat.AnthropicStreamer(model_name, emit_thinking)
    last_usage: dict = {}  # 最后一次带 usage 的事件（流读完统一记一次）
    anth_state = {"finished": False, "aborted": False, "closing": b""}
    ttft_ms = {"v": None}

    def anth_finalize() -> None:
        """收尾：统计用量 + 生成 message_stop 帧。只放在 finally 里、本身不 yield。"""
        try:
            if last_usage:
                record_usage_from(last_usage, model=str(model_name or ""),
                                  effort=str(oai_body.get("reasoning_effort") or ""),
                                  ms=(time.time() - _t0) * 1000, stream=True,
                                  ttft_ms=ttft_ms["v"])
        except Exception:  # noqa: BLE001
            LOG.exception("anthropic finalize: 用量统计失败")
        try:
            anth_state["closing"] = streamer.finish()
        except Exception:  # noqa: BLE001
            LOG.exception("anthropic finalize: 生成收尾帧失败")
        LOG.info(
            "anthropic stream done %.1fs finished=%s aborted=%s",
            time.time() - _t0, anth_state["finished"], anth_state["aborted"],
        )

    def gen():
        try:
            for ev in _upstream_sse_events(oai_body, req_model):
                if ev.startswith(b"__ERROR__"):
                    _, detail = ev.split(b":", 1)
                    yield anthropic_compat._sse(
                        "error",
                        {
                            "type": "error",
                            "error": {"type": "api_error", "message": detail.decode("utf-8", "replace")[:400]},
                        },
                    )
                    anth_state["finished"] = True
                    return
                if not ev.startswith(b"data:"):
                    continue
                payload = ev[5:].strip()
                if payload == b"[DONE]" or not payload:
                    break
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    continue
                if obj.get("usage"):
                    last_usage.clear()
                    last_usage.update(obj)
                if ttft_ms["v"] is None:
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        rc, c = d.get("reasoning_content"), d.get("content")
                        if ((isinstance(rc, str) and rc)
                                or (isinstance(c, str) and c)
                                or d.get("tool_calls")):
                            ttft_ms["v"] = (time.time() - _t0) * 1000
                            break
                if not streamer.started:
                    streamer.model = str(oai_body.get("model") or streamer.model)
                out = streamer.feed(obj)
                if out:
                    yield out
            anth_state["finished"] = True
        except GeneratorExit:
            # 客户端断开：不能再 yield（否则 "generator ignored GeneratorExit"，
            # 且 finally 之后的代码会被跳过）。收尾交给 finally + anth_finalize()。
            anth_state["aborted"] = True
            LOG.info("anthropic: client disconnected, closing upstream stream early")
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.exception("anthropic stream failed: %s", exc)
            yield anthropic_compat._sse(
                "error", {"type": "error", "error": {"type": "api_error", "message": f"上游连接失败: {exc}"}}
            )
            anth_state["finished"] = True
        finally:
            anth_finalize()

        # 正常结束 / 中途出错：补发收尾帧（客户端提前断开时走不到这里）
        if anth_state["finished"] and anth_state["closing"]:
            yield anth_state["closing"]

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/messages/count_tokens")
@app.post("/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    """粗略估算 token 数，避免 harness 因 404 报错。"""
    if not auth_ok(request):
        return _anth_error("bad local api key", 401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _anth_error("invalid json body", 400)

    text = json.dumps(body, ensure_ascii=False)
    n = max(1, int(len(text) / 3.2))
    return JSONResponse({"input_tokens": n})


def check_exposed_host(host: str) -> None:
    """非回环监听 = 把自己的校园账号开放给同网段的任何人。

    分发包里 local_api_key 的默认值是公开的（文档与代码里都写着），沿用默认 key
    对外监听等同于不设防；留空更是完全不校验。这两种情况直接拒绝启动，
    而不是带着默认 key 悄悄上线。
    """
    if host in _LOOPBACK_HOSTS:
        return
    key = (CFG.get("local_api_key") or "").strip()
    if not key or key == DEFAULT_CONFIG["local_api_key"]:
        raise SystemExit(
            f"拒绝以 {host} 启动：local_api_key 仍是公开默认值 "
            f"{DEFAULT_CONFIG['local_api_key']!r}（或为空），对外开放等于把你的校园账号"
            f"交给同网段的任何人。\n"
            f"请先在 config.json 里把 local_api_key 换成一个随机字符串；"
            f"仅本机使用时不需要改动（默认监听 127.0.0.1）。"
        )
    LOG.warning("正在监听 %s（非回环）：任何能访问该地址的人都可凭 API Key 使用你的校园账号；"
                "请确认已设置强随机 local_api_key，并建议开启 rate_limit_per_minute / max_concurrency。",
                host)


def main() -> int:
    ap = argparse.ArgumentParser(description="hainnu Open WebUI -> OpenAI proxy")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    global CFG
    CFG = load_config(Path(args.config))
    host = args.host or CFG["host"]
    port = args.port or CFG["port"]
    check_exposed_host(host)
    if CFG.get("debug_capture"):
        LOG.warning("debug_capture 已开启：客户端发来的**完整请求正文**会明文写入 "
                    "debug_capture.log，排查结束后请关闭并删除该文件。")

    # 先打启动标记：get_token/fetch_models 是联网慢操作，GUI 靠它知道进程刚起来、别提前判失败
    print(f"[booting] 初始化：拉取令牌 + 模型（可能需数秒）…", flush=True)

    try:
        get_token()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {exc}", file=sys.stderr)

    fetch_models(force=True)

    import uvicorn

    print(f"[ready] http://{host}:{port}/v1  (local api key: {CFG.get('local_api_key')})",
          flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0

if __name__ == "__main__":
    sys.exit(main())
