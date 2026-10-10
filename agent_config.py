"""把学校大模型一键写入客户端配置（ZCode / opencode / WorkBuddy / DSH）。

先自动探测本机装了哪些客户端、配置文件在哪（不写死路径），再**同时写入两条链路**：

    proxy   经本地服务    baseURL http://127.0.0.1:<port>/v1   key = config.json 的 local_api_key
    direct  直连学校      baseURL <upstream>/api               key = token.txt 里的登录 JWT

供管理台「连接配置」卡片旁的「一键配置」按钮调用；也可单独命令行运行：

    python agent_config.py --list                # 只探测：装了哪些、配置文件在哪
    python agent_config.py                       # 探测并写入全部已装客户端（两条链路）
    python agent_config.py --agent dsh --mode direct   # 只写某个客户端的某条链路

各客户端配置文件位置（按用户主目录定位，不写死盘符；opencode 兼容 XDG_CONFIG_HOME）：
    zcode     ~/.zcode/v2/config.json 与 ~/.zcode/v2/provider_config.json（两处同步写）
    opencode  ~/.config/opencode/opencode.jsonc（"provider" 与 "providers" 两段都写）
    workbuddy ~/.workbuddy/models.json（顶层数组）
    dsh       ~/.dsh/profiles/*/cordis.patch.yml（llm-pi-ai.providers，优先 desktop 档案）
              与 ~/.dsh/.credentials.yaml（apiKeyEnv 引用的密钥）

设计约束：
  * 已存在的条目只更新 baseURL / apiKey（其余字段与注释原样保留）；缺失才新建；
  * 写前备份（.bak-时间戳）、写后校验、失败自动回滚；幂等，重复执行不堆积；
  * 没获取令牌（token.txt）时直连链路整体跳过并提示，不影响本地链路；
  * 没装的客户端自动跳过，不报错；
  * 客户端只在启动时读配置：写完后要重启对应客户端。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="backslashreplace")
    except Exception:
        pass

MODEL_ID = "deepseek-flash"
CONTEXT = 300000
OUTPUT = 32768
AGENTS = ("zcode", "opencode", "workbuddy", "dsh")
AGENT_LABELS = {"zcode": "ZCode", "opencode": "opencode",
                "workbuddy": "WorkBuddy", "dsh": "DeepSeek Harness (DSH)"}
MODES = ("proxy", "direct")
MODE_LABELS = {"proxy": "经本地服务", "direct": "直连学校"}


# ---- 项目侧取值（端口 / 本地 key / 直连 JWT）--------------------------------
def home_root() -> Path:
    """用户主目录；测试可用环境变量 HAINNU_AGENTCONFIG_HOME 指到沙箱。"""
    override = os.environ.get("HAINNU_AGENTCONFIG_HOME")
    return Path(override) if override else Path(os.path.expanduser("~"))


def project_config() -> dict:
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def project_jwt() -> str:
    f = HERE / "token.txt"
    if not f.exists():
        raise RuntimeError("还没有登录令牌（token.txt）：先在管理台点「获取令牌」，"
                           "再配置直连链路")
    import token_codec  # noqa: PLC0415
    return token_codec.decrypt(f.read_text(encoding="utf-8").strip())


def endpoints(mode: str) -> tuple[str, str]:
    """返回 (baseURL, apiKey)。"""
    cfg = project_config()
    if mode == "proxy":
        port = int(cfg.get("port") or 8787)
        key = str(cfg.get("local_api_key") or "sk-hainnu")
        return f"http://127.0.0.1:{port}/v1", key
    upstream = str(cfg.get("upstream") or "").rstrip("/")
    if not upstream:
        raise RuntimeError("config.json 里没有 upstream，无法确定直连地址")
    return upstream + "/api", project_jwt()


# ---- 客户端探测（装了哪些、配置在哪）---------------------------------------
def _oc_candidates() -> list[Path]:
    out = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        out.append(Path(xdg) / "opencode" / "opencode.jsonc")
    out.append(home_root() / ".config" / "opencode" / "opencode.jsonc")
    return out


def _oc_config_path() -> Path:
    """opencode 配置：已有文件优先（兼容 XDG_CONFIG_HOME），否则用默认位置新建。"""
    for c in _oc_candidates():
        if c.exists():
            return c
    return _oc_candidates()[-1]


def _zcode_paths() -> tuple[Path, Path]:
    d = home_root() / ".zcode" / "v2"
    return d / "config.json", d / "provider_config.json"


def _dsh_creds_path() -> Path:
    return home_root() / ".dsh" / ".credentials.yaml"


def _dsh_all_patches() -> list[Path]:
    root = home_root() / ".dsh"
    out = sorted(root.glob("profiles/*/cordis.patch.yml"))
    root_patch = root / "cordis.patch.yml"
    if root_patch.exists():
        out.append(root_patch)
    return out


def _dsh_targets() -> list[Path]:
    """要写入的 cordis patch：优先 desktop 档案；否则已含 hainnu 的；再退第一个。"""
    active = []
    for p in _dsh_all_patches():
        try:
            if "- id: llm-pi-ai" in p.read_text(encoding="utf-8"):
                active.append(p)
        except Exception:  # noqa: BLE001
            continue
    if not active:
        return []
    desktop = [p for p in active if p.parent.name == "desktop"]
    if desktop:
        return desktop
    with_hainnu = []
    for p in active:
        try:
            if "hainnu" in p.read_text(encoding="utf-8"):
                with_hainnu.append(p)
        except Exception:  # noqa: BLE001
            continue
    return with_hainnu or active[:1]


def detect_agents() -> dict:
    """探测本机装了哪些客户端、配置文件在哪。返回 {agent: {found, paths, detail}}。"""
    h = home_root()
    out: dict[str, dict] = {}

    oc = _oc_config_path()
    oc_alts = [c for c in _oc_candidates() if c != oc]
    oc_dir = oc.exists() or oc.parent.is_dir() or any(c.parent.is_dir() for c in oc_alts)
    oc_found = oc_dir or bool(shutil.which("opencode"))
    out["opencode"] = {
        "found": bool(oc_found), "paths": [str(oc)],
        "detail": "已找到配置文件" if oc.exists()
                  else "找到安装目录（写入时将新建配置文件）" if oc_dir
                  else "在 PATH 上找到 opencode" if oc_found
                  else "未找到配置文件或安装目录",
    }

    wb = h / ".workbuddy" / "models.json"
    out["workbuddy"] = {
        "found": wb.exists() or wb.parent.is_dir(), "paths": [str(wb)],
        "detail": "已找到配置文件" if wb.exists()
                  else "找到安装目录（写入时将新建配置文件）" if wb.parent.is_dir()
                  else "未找到配置文件或安装目录",
    }

    patches = _dsh_targets()
    creds = _dsh_creds_path()
    out["dsh"] = {
        "found": bool(patches),
        "paths": [str(p) for p in patches] + ([str(creds)] if creds.exists() else []),
        "detail": "已找到 cordis.patch.yml" if patches
                  else "没找到带 llm-pi-ai 的 cordis.patch.yml",
    }

    zc, zpc = _zcode_paths()
    out["zcode"] = {
        "found": zc.exists(), "paths": [str(zc), str(zpc)],
        "detail": "已找到配置文件" if zc.exists()
                  else "未找到 ~/.zcode/v2/config.json",
    }
    return out


# ---- 通用：备份 / 原子写 / 结果 ---------------------------------------------
def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    return bak


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _res(agent: str, action: str, ok: bool = True, msg: str = "",
         paths=(), backup: Path | None = None) -> dict:
    return {"agent": agent, "ok": ok, "action": action, "msg": msg,
            "paths": [str(p) for p in paths],
            "backup": str(backup) if backup else ""}


def _save(path: Path, new: str, old: str, agent: str, action: str,
          msg: str, validate) -> dict:
    """公共写回：无变化跳过；有变化则备份 → 校验 → 原子写 → 复读校验 → 回滚。"""
    if new == old:
        return _res(agent, "skipped", True, "已是最新，无需改动", [path])
    bak = _backup(path)
    try:
        validate(new)
        _atomic_write(path, new)
        validate(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        if bak and bak.exists():
            shutil.copy2(bak, path)
        return _res(agent, "error", False, f"写入校验未通过（已回滚）：{exc}",
                    [path], bak)
    return _res(agent, action, True, msg, [path], bak)


# ---- JSONC 定点处理（opencode.jsonc 用；保留注释）---------------------------
def _strip_jsonc(t: str) -> str:
    out, i, n = [], 0, len(t)
    in_str = esc = False
    while i < n:
        c = t[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and t[i + 1] == "/":
            while i < n and t[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and t[i + 1] == "*":
            i += 2
            while i + 1 < n and not (t[i] == "*" and t[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _match_brace(t: str, open_i: int) -> int:
    depth, in_str, esc = 0, False, False
    i = open_i
    while i < len(t):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError("大括号不配对")


def _sec_span(t: str, key: str):
    """顶层缩进 2 的空格段 `  "key": { ... }` 的 (行首, '{', '}')。"""
    m = re.search(r'(?m)^  "%s"\s*:\s*\{' % re.escape(key), t)
    if not m:
        return None
    open_i = t.rindex("{", m.start(), m.end())
    return m.start(), open_i, _match_brace(t, open_i)


def _entry_span(t: str, lo: int, hi: int, pid: str):
    m = re.search(r'(?m)^([ \t]*)"%s"\s*:\s*\{' % re.escape(pid), t[lo:hi])
    if not m:
        return None
    open_i = lo + m.start() + m.group(0).rindex("{")
    return lo + m.start(), _match_brace(t, open_i) + 1


def _json_field_patch(text: str, start: int, end: int,
                      base: str | None = None, key: str | None = None):
    seg = orig = text[start:end]
    if base is not None:
        seg = re.sub(r'("baseURL"\s*:\s*)"(?:[^"\\]|\\.)*"',
                     lambda m: m.group(1) + json.dumps(base, ensure_ascii=False),
                     seg, count=1)
    if key is not None:
        seg = re.sub(r'("apiKey"\s*:\s*)"(?:[^"\\]|\\.)*"',
                     lambda m: m.group(1) + json.dumps(key, ensure_ascii=False),
                     seg, count=1)
    if seg == orig:
        return text, False
    return text[:start] + seg + text[end:], True


def _json_cut_entry(text: str, start: int, end: int) -> str:
    tail = text[end:]
    for pat in (r",[ \t]*\n", r","):
        m = re.match(pat, tail)
        if m:
            return text[:start] + tail[m.end():]
    head = text[:start]
    m = re.search(r",[ \t]*\n[ \t]*\Z", head)
    if m:
        return head[:m.start()] + tail
    return head + tail


def _entry_json(pid: str, block: dict, indent: int = 4) -> str:
    """`"pid": { ... }` 单条目的多行文本（顶层键落在 indent 列）。"""
    body = json.dumps({pid: block}, ensure_ascii=False, indent=2)
    pad = " " * (indent - 2)
    inner = body.splitlines()[1:-1]
    return "\n".join(pad + ln if ln.strip() else ln for ln in inner)


# ---- opencode ---------------------------------------------------------------
def _oc_target(mode: str):
    if mode == "proxy":
        return "hainnu", "Hainnu", "DeepSeek V4.1 Flash (海师)"
    return ("hainnu-direct", "Hainnu 直连",
            "DeepSeek V4.1 Flash (海师直连)")


def _oc_block_dict(pid: str, pname: str, mname: str, base: str, key: str,
                   plural: bool) -> dict:
    if plural:
        return {
            "name": pname,
            "package": "aisdk:@ai-sdk/openai-compatible",
            "settings": {"baseURL": base, "apiKey": key, "timeout": 600000},
            "models": {MODEL_ID: {
                "name": mname,
                "capabilities": {"tools": True,
                                 "input": ["text", "image"],
                                 "output": ["text"]},
                "compatibility": {"reasoningField": "reasoning_content"},
                "limit": {"context": CONTEXT, "output": OUTPUT},
                "settings": {"reasoningEffort": "max"},
                "variants": [
                    {"id": "none", "settings": {"reasoningEffort": "none"}},
                    {"id": "low", "settings": {"reasoningEffort": "low"}},
                    {"id": "max", "settings": {"reasoningEffort": "max"}},
                ],
            }},
        }
    return {
        "name": pname,
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": base, "apiKey": key, "timeout": 600000},
        "models": {MODEL_ID: {
            "name": mname,
            "tool_call": True,
            "reasoning": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "attachment": True,
            "limit": {"context": CONTEXT, "output": OUTPUT},
            "options": {"reasoningEffort": "max"},
            "variants": {
                "none": {"reasoningEffort": "none"},
                "low": {"reasoningEffort": "low"},
                "max": {"reasoningEffort": "max"},
            },
        }},
    }


def _oc_apply_section(text: str, sec: str, pid: str, block: dict,
                      base: str, key: str, create_root: bool) -> str:
    entry = _entry_json(pid, block, 4)
    span = _sec_span(text, sec)
    if span is None:
        if not create_root:
            return text
        stripped = text.rstrip()
        idx = stripped.rfind("}")
        if idx <= 0:
            return '{\n  "%s": {\n%s\n  }\n}\n' % (sec, entry)
        head = stripped[:idx].rstrip()
        comma = "" if head.endswith(("{", "[", ",")) else ","
        return (head + comma + '\n  "%s": {\n' % sec + entry + "\n  }\n}\n")
    _, open_i, close_i = span
    ent = _entry_span(text, open_i + 1, close_i, pid)
    if ent:
        patched, changed = _json_field_patch(text, ent[0], ent[1], base, key)
        return patched if changed else text
    ins = open_i + 1
    return text[:ins] + "\n" + entry + "," + text[ins:]


def set_opencode(mode: str) -> dict:
    path = _oc_config_path()
    fresh = not path.exists()
    if fresh and not (path.parent.is_dir() or shutil.which("opencode")):
        return _res("opencode", "error", False,
                    f"未检测到 opencode（{path} 不存在）")
    old = "{}\n" if fresh else path.read_text(encoding="utf-8")
    if not fresh:
        try:
            json.loads(_strip_jsonc(old))
        except Exception as exc:  # noqa: BLE001
            return _res("opencode", "error", False, f"现有文件不是合法 JSONC：{exc}")

    def validate(t: str):
        json.loads(_strip_jsonc(t))

    base, key = endpoints(mode)
    pid, pname, mname = _oc_target(mode)
    text = old
    for sec in ("provider", "providers"):
        text = _oc_apply_section(text, sec, pid,
                                 _oc_block_dict(pid, pname, mname, base, key,
                                                plural=(sec == "providers")),
                                 base, key,
                                 create_root=(sec == "provider" or fresh))
    before = len(re.findall(r'(?m)^[ \t]*"%s"' % re.escape(pid), old))
    after = len(re.findall(r'(?m)^[ \t]*"%s"' % re.escape(pid), text))
    action = "created" if (fresh or after > before) else "updated"
    msg = f"已写入 {pid}（baseURL/apiKey）"
    if fresh:
        msg = f"已新建 opencode.jsonc 并写入 {pid}"
    return _save(path, text, old, "opencode", action, msg, validate)


# ---- WorkBuddy --------------------------------------------------------------
def _wb_target(mode: str):
    if mode == "proxy":
        return ("hainnu/deepseek-flash",
                "Hainnu / DeepSeek V4.1 Flash (海师)")
    return ("hainnu-direct/deepseek-flash",
            "Hainnu 直连 / DeepSeek V4.1 Flash (海师直连)")


def _wb_new(mid: str, name: str, base: str, key: str) -> dict:
    return {
        "id": mid, "name": name, "vendor": "openai", "url": base, "apiKey": key,
        "maxInputTokens": CONTEXT, "maxOutputTokens": OUTPUT,
        "supportsToolCall": True, "supportsImages": True,
        "useCustomProtocol": False, "supportsReasoning": True,
        "onlyReasoning": False,
        "reasoning": {"effort": "max", "defaultEffort": "max",
                      "supportedEfforts": ["low", "max"],
                      "summary": "auto", "canDisableThinking": True},
        "thinkingLevelMap": {"off": "off", "minimal": None, "low": "low",
                             "medium": None, "high": None, "xhigh": None,
                             "max": "max"},
    }


def set_workbuddy(mode: str) -> dict:
    path = home_root() / ".workbuddy" / "models.json"
    if path.exists():
        old = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(old) if old.strip() else []
        except Exception as exc:  # noqa: BLE001
            return _res("workbuddy", "error", False, f"现有文件不是合法 JSON：{exc}")
    elif path.parent.is_dir():
        old, parsed = "[]\n", []
    else:
        return _res("workbuddy", "error", False,
                    f"未检测到 WorkBuddy（{path} 不存在）")
    dict_mode = isinstance(parsed, dict)
    models = parsed.get("models") if dict_mode else parsed
    if not isinstance(models, list):
        return _res("workbuddy", "error", False,
                    "顶层既不是数组、也没有 models 列表，未改动")

    def dump() -> str:
        if dict_mode:
            parsed["models"] = models
            return json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
        return json.dumps(models, ensure_ascii=False, indent=2) + "\n"

    def validate(t: str):
        json.loads(t)

    base, key = endpoints(mode)
    mid, name = _wb_target(mode)
    entry = next((m for m in models
                  if isinstance(m, dict) and m.get("id") == mid), None)
    if entry is not None:
        entry["url"] = base
        entry["apiKey"] = key
        action = "updated"
    else:
        models.append(_wb_new(mid, name, base, key))
        action = "created"
    return _save(path, dump(), old, "workbuddy", action,
                 f"已写入 {mid}（url/apiKey）", validate)


# ---- DeepSeek Harness（cordis patch + credentials）--------------------------
DSH_ENV = {"proxy": "HAINNU_API_KEY", "direct": "HAINNU_DIRECT_API_KEY"}


def _dsh_target(mode: str):
    if mode == "proxy":
        return "hainnu", "DeepSeek V4.1 Flash (海师)"
    return "hainnu-direct", "DeepSeek V4.1 Flash (海师直连)"


def _dsh_block(pid: str, disp: str, env: str, base: str, ctx: int,
               mname: str) -> str:
    return (
        f"      {pid}:\n"
        f"        displayName: {disp}\n"
        f"        apiKeyEnv: {env}\n"
        f"        api: openai-completions\n"
        f"        baseURL: {base}\n"
        f"        models:\n"
        f"          - id: {MODEL_ID}\n"
        f"            name: {mname}\n"
        f"            contextWindow: {ctx}\n"
        f"            maxTokens: {OUTPUT}\n"
        f"            input:\n"
        f"              - text\n"
        f"              - image\n"
        f"            reasoningEfforts:\n"
        f"              off: none\n"
        f"              low: low\n"
        f"              max: max\n"
        f"        compat:\n"
        f"          thinkingFormat: deepseek\n"
        f"          supportsDeveloperRole: false\n"
        f"          maxTokensField: max_tokens\n"
        f"        reasoning: max\n"
    )


def _dsh_entry_span(text: str):
    m = re.search(r"(?m)^- id: llm-pi-ai[ \t]*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^- ", text[m.end():])
    end = m.end() + nxt.start() if nxt else len(text)
    return m.start(), end


def _dsh_provider_spans(seg: str) -> dict[str, tuple[int, int]]:
    """llm-pi-ai 条目里 `config.providers` 下各 provider 块的 (起, 止) 偏移。"""
    m = re.search(r"(?m)^ {4}providers:\s*$", seg)
    if not m:
        return {}
    lines, pos = [], 0
    for ln in seg.splitlines(keepends=True):
        lines.append((pos, len(pos and ln or ln), ln))
        pos += len(ln)
    # 找 providers 冒号行在 lines 里的下标
    start_line = None
    for i, (off, _l, ln) in enumerate(lines):
        if off == m.start():
            start_line = i
            break
    if start_line is None:
        return {}
    end_line = len(lines)
    for j in range(start_line + 1, len(lines)):
        ln = lines[j][2]
        if ln.strip() and not ln.lstrip().startswith(("#", "-")) \
                and (len(ln) - len(ln.lstrip(" "))) <= 4:
            end_line = j
            break
    prov_end = lines[end_line][0] if end_line < len(lines) else len(seg)
    spans: dict[str, tuple[int, int]] = {}
    i = start_line + 1
    while i < end_line:
        ln = lines[i][2]
        mm = re.match(r"^( {6})([^\s:#][^:]*?):\s*(#.*)?$", ln)
        if mm:
            key = mm.group(2).strip()
            j = i + 1
            while j < end_line:
                ln2 = lines[j][2]
                if ln2.strip() and not ln2.lstrip().startswith(("#", "-")) \
                        and (len(ln2) - len(ln2.lstrip(" "))) <= 6:
                    break
                j += 1
            stop = lines[j][0] if j < end_line else prov_end
            spans[key] = (lines[i][0], stop)
            i = j
        else:
            i += 1
    return spans


def _dsh_patch_baseurl(seg: str, base: str) -> tuple[str, bool]:
    new = re.sub(r"(?m)^( {8}baseURL:\s*).*$", lambda m: m.group(1) + base,
                 seg, count=1)
    return new, new != seg


def _yaml_scalar(v: str) -> str:
    if v and re.fullmatch(r"[A-Za-z0-9._~+/=@()\-]+", v):
        return v
    return "'" + v.replace("'", "''") + "'"


def _creds_set(text: str, var: str, value: str) -> str:
    pat = r"(?m)^(  %s:\s*).*$" % re.escape(var)
    scalar = _yaml_scalar(value)
    if re.search(pat, text):
        return re.sub(pat, lambda m: m.group(1) + scalar, text, count=1)
    m = re.search(r"(?m)^refs:\s*$", text)
    if m:
        return text[:m.end()] + "\n  %s: %s" % (var, scalar) + text[m.end():]
    return text.rstrip() + "\nrefs:\n  %s: %s\n" % (var, scalar)


def _set_user_env(var: str, value: str) -> bool:
    """写入 HKCU 环境变量并广播；值本来就一致时不动、返回 False。"""
    try:
        import ctypes  # noqa: PLC0415
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            try:
                cur, _t = winreg.QueryValueEx(k, var)
            except FileNotFoundError:
                cur = None
            if cur == value:
                return False
            winreg.SetValueEx(k, var, 0, winreg.REG_SZ, value)
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
        return True
    except Exception:  # noqa: BLE001
        return False


def _dsh_validate(t: str):
    if "- id: llm-pi-ai" not in t:
        raise ValueError("缺少 llm-pi-ai 段落")
    for ln in t.splitlines():
        s = ln.lstrip(" ")
        if s and not s.startswith("#") and (len(ln) - len(s)) % 2:
            raise ValueError("YAML 缩进异常（出现奇数个空格）")
    try:
        import yaml  # noqa: PLC0415
        yaml.safe_load(t)
    except ImportError:
        pass


def set_dsh(mode: str) -> dict:
    patches = _dsh_targets()
    if not patches:
        return _res("dsh", "error", False,
                    "未检测到 DSH（没找到带 llm-pi-ai 的 cordis.patch.yml）")
    base, key = endpoints(mode)
    env = DSH_ENV[mode]
    ctx = 210000 if mode == "proxy" else CONTEXT
    mname = ("DeepSeek V4.1 Flash (海师)" if mode == "proxy"
             else "DeepSeek V4.1 Flash (海师直连)")
    pid, disp = _dsh_target(mode)

    results = []
    for path in patches:
        old = path.read_text(encoding="utf-8")
        span = _dsh_entry_span(old)
        if span is None:
            results.append(_res("dsh", "error", False,
                                f"{path} 里没有 `- id: llm-pi-ai` 段落"))
            continue
        seg = old[span[0]:span[1]]
        provs = _dsh_provider_spans(seg)
        text = old
        action = "updated"
        if pid in provs:
            s, e = provs[pid]
            newblock, changed = _dsh_patch_baseurl(seg[s:e], base)
            if changed:
                text = text[:span[0] + s] + newblock + text[span[0] + e:]
        else:
            block = _dsh_block(pid, disp, env, base, ctx, mname)
            # 插到 providers 段末尾（即最后一个 provider 块之后）
            if provs:
                last_end = max(e for _s2, e in provs.values())
                ins = span[0] + last_end
            else:
                pm = re.search(r"(?m)^ {4}providers:\s*\n", seg)
                ins = span[0] + pm.end() if pm else span[1]
            if not text[:ins].endswith("\n"):
                block = "\n" + block
            text = text[:ins] + block + text[ins:]
            action = "created"
        results.append(_save(path, text, old, "dsh", action,
                             f"已写入 {pid}（baseURL）", _dsh_validate))

    creds_note = ""
    creds = _dsh_creds_path()
    if creds.exists():
        ct = creds.read_text(encoding="utf-8")
        nt = _creds_set(ct, env, key)
        if nt != ct:
            _backup(creds)
            _atomic_write(creds, nt)
            creds_note = f"；密钥已写入 {creds.name}"
    if _set_user_env(env, key):
        creds_note += f"；环境变量 {env} 已刷新"

    ok = all(r["ok"] for r in results)
    multi = len(results) > 1
    msgs, paths, backs = [], [], []
    for r in results:
        m = r["msg"]
        if multi and r["paths"]:
            m = f"{Path(r['paths'][0]).parent.name}: {m}"
        msgs.append(m)
        paths.extend(r["paths"])
        if r["backup"]:
            backs.append(r["backup"])
    if not ok:
        action = "error"
    elif all(r["action"] == "skipped" for r in results):
        action = "skipped"
    elif any(r["action"] == "created" for r in results):
        action = "created"
    else:
        action = "updated"
    res = _res("dsh", action, ok, "；".join(msgs) + creds_note, paths)
    res["backup"] = "；".join(backs)
    return res


# ---- ZCode ------------------------------------------------------------------
def _z_target(mode: str):
    if mode == "proxy":
        return "Hainnu", "DeepSeek V4.1 Flash (海师)"
    return "Hainnu 直连", "DeepSeek V4.1 Flash (海师直连)"


def _z_find_uuid(prov: dict, name: str, base: str):
    for uid, ent in prov.items():
        if not isinstance(ent, dict):
            continue
        ename = str(ent.get("name") or "")
        opts = ent.get("options") or {}
        eurl = str(opts.get("baseURL") or "").rstrip("/")
        if eurl == base.rstrip("/") or ename == name:
            return uid
    return None


def _z_rules(pcfg: dict) -> tuple[list, list]:
    conf = pcfg.setdefault("config", {})
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    models = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
    return rules, models


def _z_new_config_entry(name: str, mname: str, base: str, key: str) -> dict:
    return {
        "name": name,
        "kind": "openai-compatible",
        "options": {"apiKey": key, "baseURL": base, "apiKeyRequired": True},
        "enabled": True,
        "source": "custom",
        "models": {MODEL_ID: {
            "name": mname,
            "reasoning": {"enabled": True, "variants": ["off", "low", "max"],
                          "defaultVariant": "max"},
            "limit": {"context": CONTEXT, "output": OUTPUT},
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "zcode": {"modalitiesConfigured": True, "modified": True},
        }},
    }


def _z_new_rule(uid: str, name: str, base: str, key: str) -> dict:
    return {
        "providerId": uid, "providerName": name, "enabled": True,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": key},
            "api": {"type": "openai-chat-completions", "baseUrl": base},
            "personalModelIds": [MODEL_ID],
            "modelOrder": [MODEL_ID],
        },
    }


def set_zcode(mode: str) -> dict:
    cpath, ppath = _zcode_paths()
    if not cpath.exists():
        return _res("zcode", "error", False,
                    f"未检测到 ZCode（{cpath} 不存在）")
    c_old = cpath.read_text(encoding="utf-8")
    try:
        cfg = json.loads(c_old)
    except Exception as exc:  # noqa: BLE001
        return _res("zcode", "error", False, f"config.json 不是合法 JSON：{exc}")
    fresh_p = not ppath.exists()
    if fresh_p:
        p_old, pcfg = "{}\n", {}
    else:
        p_old = ppath.read_text(encoding="utf-8")
        try:
            pcfg = json.loads(p_old)
        except Exception as exc:  # noqa: BLE001
            return _res("zcode", "error", False,
                        f"provider_config.json 不是合法 JSON：{exc}")
    prov = cfg.setdefault("provider", {})
    rules, mrules = _z_rules(pcfg)

    def validate_c(t: str):
        json.loads(t)

    def validate_p(t: str):
        json.loads(t)

    base, key = endpoints(mode)
    name, mname = _z_target(mode)
    uid = _z_find_uuid(prov, name, base)
    if uid:
        ent = prov[uid]
        opts = ent.setdefault("options", {})
        opts["baseURL"] = base
        opts["apiKey"] = key
        rule = next((r for r in rules
                     if r.get("providerId") == uid
                     or r.get("providerName") == name), None)
        if rule is None:
            rules.append(_z_new_rule(uid, name, base, key))
        else:
            rconf = rule.setdefault("config", {})
            rconf.setdefault("access", {})["apiKey"] = key
            rconf.setdefault("api", {})["baseUrl"] = base
        action = "updated"
    else:
        uid = str(uuid.uuid4())
        prov[uid] = _z_new_config_entry(name, mname, base, key)
        rules.append(_z_new_rule(uid, name, base, key))
        mrules.append({"modelId": MODEL_ID,
                       "config": {"properties": {"contextWindow": CONTEXT}},
                       "providerId": uid})
        action = "created"
    c_new = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    p_new = json.dumps(pcfg, ensure_ascii=False, indent=2) + "\n"
    r1 = _save(cpath, c_new, c_old, "zcode", action,
               f"已写入 {name}（options.baseURL/apiKey）", validate_c)
    r2 = _save(ppath, p_new, p_old, "zcode", action,
               "provider_config.json 已同步", validate_p)
    r1["paths"] = [str(cpath), str(ppath)]
    r1["backup"] = "；".join(x for x in (r1["backup"], r2["backup"]) if x)
    if fresh_p and r2["action"] != "skipped":
        r1["msg"] += "（已新建 provider_config.json）"
    if not r1["ok"] or not r2["ok"]:
        r1["ok"] = False
        r1["msg"] += f"（第二处：{r2['msg']}）"
    return r1


# ---- 调度与命令行 -----------------------------------------------------------
APPLIERS = {"opencode": set_opencode, "workbuddy": set_workbuddy,
            "dsh": set_dsh, "zcode": set_zcode}


def apply_many(agents, modes=MODES) -> list[dict]:
    det = detect_agents()
    token_missing = ("direct" in modes) and not (HERE / "token.txt").exists()
    out = []
    for a in agents:
        if a not in APPLIERS:
            out.append(_res(a, "error", False, f"不支持的客户端：{a}"))
            continue
        if not det.get(a, {}).get("found"):
            out.append(_res(a, "error", False,
                            f"未检测到（{det.get(a, {}).get('detail', '')}）"))
            continue
        for m in modes:
            if m == "direct" and token_missing:
                out.append(_res(a, "skipped", True,
                                "还没有登录令牌，直连链路已跳过"
                                "（先在管理台获取令牌，再重新配置）"))
                continue
            try:
                out.append(APPLIERS[a](m))
            except Exception as exc:  # noqa: BLE001
                out.append(_res(a, "error", False,
                                f"{MODE_LABELS.get(m, m)}：{exc}"))
    return out


def _print_result(r: dict) -> None:
    mark = {"skipped": "--", "cleared": "OK", "created": "OK",
            "updated": "OK"}.get(r["action"], "!!")
    line = "[%s] %-9s %s" % (mark, AGENT_LABELS.get(r["agent"], r["agent"]), r["msg"])
    print(line)
    for p in r["paths"]:
        print("      → " + p)
    if r["backup"]:
        print("      备份：" + r["backup"])


def _print_list() -> None:
    det = detect_agents()
    for a in AGENTS:
        d = det[a]
        print("%s %s" % ("[已安装]" if d["found"] else "[未找到]",
                         AGENT_LABELS[a]))
        for p in d["paths"]:
            print("   " + p)
        if not d["found"]:
            print("   （%s）" % d["detail"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="探测本机已安装的客户端，把学校大模型写入它们的配置"
                    "（本地服务 + 直连学校 两条链路）")
    ap.add_argument("--agent", default="all", choices=["all", *AGENTS])
    ap.add_argument("--mode", default="both", choices=["both", *MODES],
                    help="both=两条链路都写（默认）")
    ap.add_argument("--list", action="store_true",
                    help="只探测：各客户端是否安装、配置文件在哪")
    args = ap.parse_args()
    if args.list:
        _print_list()
        return 0
    det = detect_agents()
    agents = ([a for a in AGENTS if det[a]["found"]]
              if args.agent == "all" else [args.agent])
    if not agents:
        print("没有探测到任何已安装的客户端；--list 可查看探测详情。")
        return 1
    modes = MODES if args.mode == "both" else (args.mode,)
    results = apply_many(agents, modes)
    print()
    for r in results:
        _print_result(r)
    print()
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
