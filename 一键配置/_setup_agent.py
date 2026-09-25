"""把学校大模型一键配置到多个 Agent 客户端的**配置文件**里。

支持的目标（`--agent`）：
    opencode   →  opencode.jsonc        （JSONC，带注释，不能整体反序列化）
    workbuddy  →  models.json           （JSON：{"models": [...], "availableModels": [...]}）
    dsh        →  settings.yaml         （YAML：llm-pi-ai.providers.<id>）
    zcode      →  ~/.zcode/v2/config.json（纯 JSON：provider.<uuid>，可整体读写）
支持的两条链路（`--mode`）：
    proxy     →  经本项目的本地服务（Base URL 127.0.0.1:<port>/v1，本地 key）
    direct     →  直连学校（Base URL <upstream>/api，学校登录 JWT）

设计约束（重要）：
  * **不写死任何安装位置**：客户端装在哪、装在哪个盘都与本脚本无关，它只按
    「用户主目录 + 官方环境变量（XDG_CONFIG_HOME / WORKBUDDY_CONFIG_DIR / …）」
    推算配置文件位置；算不出来就让使用者自己指定路径（`--config` 或交互输入）。
  * **不破坏原有配置**：opencode.jsonc 与 settings.yaml 里通常有大量注释，
    一律做**定点文本替换/插入**，绝不整体反序列化再 dump。
  * **幂等**：同一目标重复执行只是更新自己的 provider/模型块，不会堆积副本。
  * **可回滚**：写前备份（带时间戳），写后校验，校验不过自动回滚。
  * 只写配置文件；不启动/不安装/不改动客户端本身。

⚠️ **配置完成 ≠ 马上能用**。真正跑起来还缺前置条件，脚本开头会检查并逐条提示：
    · 两条链路都需要「已获取登录令牌」（1.获取令牌.bat）——直连还要把 JWT 写进客户端配置；
    · 经本地服务的链路还要求「本地代理正在运行」（2.启动代理.bat）；
    · 客户端只在启动时读配置，改完必须**重启客户端**。

用法（一般由本目录里的 7 个 bat 调用）：
    python _setup_agent.py --agent opencode  --mode proxy
    python _setup_agent.py --agent workbuddy --mode direct
    python _setup_agent.py --agent dsh       --mode proxy  --config <配置文件路径>
    python _setup_agent.py --agent zcode     --mode direct  --yes
    python _setup_agent.py --agent opencode  --mode direct  --dry-run   # 只看不改
    python _setup_agent.py --agent dsh       --mode direct  --refresh-env   # 令牌轮换后只刷新 DSH 环境变量
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

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
BASE = HERE.parent                      # 项目根目录（config.json、token.txt 在这里）
sys.path.insert(0, str(BASE))

# ---- 部署内容 ---------------------------------------------------------------
# 模型名只是「客户端侧显示/请求用的名字」，实际由服务端解析到上游真实 id（见 README §7.1）。
MODEL_ID = "deepseek-flash"
MODEL_NAME_PROXY = "DeepSeek Flash（海师·经本地服务）"
MODEL_NAME_DIRECT = "DeepSeek Flash（海师·直连）"
CONTEXT = 260000          # 实测：最坏密度下 1MiB 请求体 ≈ 28.9 万，留 10% 余量
OUTPUT = 32768            # 实测 max_tokens=32768 被上游接受


def load_project_config() -> dict:
    """读项目根 config.json（上游地址、端口、本地 key 都从它来，不写死）。"""
    f = BASE / "config.json"
    if not f.exists():
        raise SystemExit(f"[错误] 找不到项目配置：{f}\n  请在完整解压的 hainnu-proxy 目录里运行。")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[错误] config.json 无法解析：{exc}")


def load_jwt() -> str:
    """直连需要学校登录 JWT：从项目里加密的 token.txt 解出。"""
    f = BASE / "token.txt"
    if not f.exists():
        raise SystemExit(
            "[错误] 还没有令牌：找不到 token.txt。\n"
            "  请先在项目根目录双击「1.获取令牌.bat」登录一次，再回来配置直连。\n"
            "  （经本地服务的那条链路不需要令牌，可直接配置。）"
        )
    try:
        import token_codec  # noqa: PLC0415
        return token_codec.decrypt(f.read_text(encoding="utf-8").strip())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[错误] 令牌解密失败（{exc}）。请重跑「1.获取令牌.bat」。")


def endpoints(cfg: dict, mode: str) -> tuple[str, str]:
    """返回 (baseURL, apiKey)。"""
    if mode == "proxy":
        port = cfg.get("port", 8787)
        key = cfg.get("local_api_key") or "sk-hainnu"
        return f"http://127.0.0.1:{port}/v1", key
    upstream = (cfg.get("upstream") or "").rstrip("/")
    if not upstream:
        raise SystemExit("[错误] config.json 里没有 upstream，无法确定直连地址。")
    return upstream + "/api", load_jwt()


# ---- 配置文件定位（只按用户主目录与官方环境变量推算）------------------------
def home() -> Path:
    return Path(os.path.expanduser("~"))


def candidates(agent: str) -> list[tuple[str, Path]]:
    """(说明, 路径) 候选列表。全部基于 ~ 与环境变量，不含任何写死的盘符/用户名。"""
    h = home()
    env = os.environ
    out: list[tuple[str, Path]] = []

    if agent == "opencode":
        if env.get("OPENCODE_CONFIG"):
            out.append(("环境变量 OPENCODE_CONFIG", Path(env["OPENCODE_CONFIG"])))
        xdg = env.get("XDG_CONFIG_HOME")
        if xdg:
            out.append(("XDG_CONFIG_HOME", Path(xdg) / "opencode" / "opencode.jsonc"))
        out.append(("用户级默认", h / ".config" / "opencode" / "opencode.jsonc"))
        out.append(("当前目录（项目级）", Path.cwd() / "opencode.jsonc"))

    elif agent == "workbuddy":
        for var in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
            if env.get(var):
                out.append((f"环境变量 {var}", Path(env[var]) / "models.json"))
        out.append(("用户级（WorkBuddy）", h / ".workbuddy" / "models.json"))
        out.append(("用户级（CodeBuddy）", h / ".codebuddy" / "models.json"))
        out.append(("当前目录（项目级）", Path.cwd() / ".codebuddy" / "models.json"))

    elif agent == "dsh":
        if env.get("DSH_CONFIG_DIR"):
            out.append(("环境变量 DSH_CONFIG_DIR", Path(env["DSH_CONFIG_DIR"]) / "settings.yaml"))
        xdg = env.get("XDG_CONFIG_HOME")
        if xdg:
            out.append(("XDG_CONFIG_HOME", Path(xdg) / "dsh" / "settings.yaml"))
        out.append(("用户级默认", h / ".dsh" / "settings.yaml"))

    elif agent == "zcode":
        if env.get("ZCODE_CONFIG_DIR"):
            out.append(("环境变量 ZCODE_CONFIG_DIR", Path(env["ZCODE_CONFIG_DIR"]) / "config.json"))
        out.append(("用户级默认", h / ".zcode" / "v2" / "config.json"))

    return out


def looks_right(agent: str, path: Path) -> bool:
    """宽松判断：这个文件像不像是目标客户端的配置（不作为硬性门槛）。"""
    try:
        t = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return False
    if agent == "opencode":
        return "provider" in t or "model" in t
    if agent == "workbuddy":
        return t.strip().startswith(("[", "{"))
    if agent == "dsh":
        return "settings" in t or ":" in t
    if agent == "zcode":
        return '"provider"' in t or '"models"' in t
    return True


def pick_config(agent: str, given: str | None) -> Path:
    """定位配置文件：--config > 自动候选 > 交互输入。"""
    if given:
        p = Path(given).expanduser()
        if not p.exists():
            raise SystemExit(f"[错误] 指定的配置文件不存在：{p}")
        return p

    found = [(d, p) for d, p in candidates(agent) if p.exists()]
    print("\n正在寻找 %s 的配置文件…" % agent)
    for d, p in candidates(agent):
        print("   %s  %s  %s" % ("[存在]" if p.exists() else "[ 无 ]", p, d))

    if found:
        if len(found) == 1:
            p = found[0][1]
            print("\n使用：%s" % p)
            return p
        print("\n找到多个候选，请选择：")
        for i, (d, p) in enumerate(found, 1):
            print("   %d) %s   （%s）" % (i, p, d))
        print("   %d) 手动输入其它路径" % (len(found) + 1))
        sel = input("请输入序号后回车：").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(found):
            return found[int(sel) - 1][1]

    print("\n没有自动找到配置文件。")
    print("请把配置文件**拖拽**进本窗口后回车，或手动输入完整路径，")
    print("也可以直接回车 → 由本脚本在该位置新建一份。")
    raw = input("路径：").strip().strip('"').strip("'")
    if not raw:
        default = candidates(agent)[-1][1]
        print("将新建：%s" % default)
        default.parent.mkdir(parents=True, exist_ok=True)
        return default
    p = Path(raw).expanduser()
    if p.is_dir():
        p = p / {"opencode": "opencode.jsonc", "workbuddy": "models.json",
                 "dsh": "settings.yaml", "zcode": "config.json"}[agent]
    if not p.exists():
        print("（该文件尚不存在，将新建：%s）" % p)
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not looks_right(agent, p):
        ask = input("这个文件看起来不像 %s 的配置，仍要写入？[y/N] " % agent).strip().lower()
        if ask != "y":
            raise SystemExit("已取消，未做任何改动。")
    return p


# ---- 通用：备份 / 写回 / 回滚 ----------------------------------------------
def backup(path: Path) -> Path:
    if not path.exists():
        return None  # type: ignore[return-value]
    bak = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    return bak


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def set_user_env(var: str, value: str) -> tuple[bool, str]:
    """写入用户级环境变量（HKCU\\Environment）并广播系统变更。

    返回 (是否成功, 失败原因)。成功后**新启动**的进程即可读到；
    已经在运行的进程（含旧终端）不受影响，需重启客户端。
    """
    try:
        import winreg  # noqa: PLC0415
        import ctypes  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, var, 0, winreg.REG_SZ, value)
        # 广播 WM_SETTINGCHANGE，让资源管理器等宿主刷新环境块
        HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, None)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def strip_jsonc(t: str) -> str:
    """去掉 // 与 /* */ 注释和尾逗号，便于用 json.loads 校验（不用于写回）。"""
    out, i, n = [], 0, len(t)
    in_str = False
    esc = False
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
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def validate(agent: str, text: str) -> None:
    if agent == "opencode":
        json.loads(strip_jsonc(text))
    elif agent == "workbuddy":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("顶层必须是对象 {\"models\": [...]}（原来是数组会被客户端忽略）")
    elif agent == "zcode":
        data = json.loads(strip_jsonc(text))
        if not isinstance(data, dict) or not isinstance(data.get("provider"), dict):
            raise ValueError("顶层必须含 provider 对象")
    elif agent == "dsh":
        try:
            import yaml  # noqa: PLC0415
            yaml.safe_load(text)
        except ImportError:
            # runtime 里没有 pyyaml：退化为结构性检查（键存在 + 缩进对齐）
            if "llm-pi-ai:" not in text:
                raise ValueError("缺少 llm-pi-ai 段")
            for line in text.splitlines():
                if line.strip() and not line.startswith((" ", "#", "-")) and ":" in line:
                    pass
            for line in text.splitlines():
                stripped = line.lstrip(" ")
                if stripped and not stripped.startswith(("#", "-")) and (len(line) - len(stripped)) % 2:
                    raise ValueError("YAML 缩进异常（可能插错了层级）")


def apply_change(agent: str, path: Path, new_text: str, dry: bool) -> None:
    if dry:
        # JWT / key 不打印到屏幕：直连模式下 new_text 里含明文令牌
        masked = re.sub(r'("(?:apiKey|api_key)"\s*:\s*)"[^"]*"',
                        r'\1"***（已隐藏）***"', new_text)
        print("\n[dry-run] 以下内容将写入 %s（未实际改动，密钥已隐藏）：\n" % path)
        print(masked)
        return
    bak = backup(path)
    try:
        validate(agent, new_text)
        write_atomic(path, new_text)
        # 写后再读一次做最终确认
        validate(agent, path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        if bak and bak.exists():
            shutil.copy2(bak, path)
            print("\n[已回滚] 写入校验未通过：%s\n  原文件已从备份恢复：%s" % (exc, bak.name))
        else:
            print("\n[失败] 写入校验未通过：%s" % exc)
        raise SystemExit(1)
    if bak:
        print("\n已备份原文件：%s" % bak.name)


# ---- opencode（JSONC 定点处理）---------------------------------------------
def opencode_block(pid: str, name: str, base: str, key: str, mode: str) -> str:
    effort = "low" if mode == "proxy" else "low"
    return f'''    "{pid}": {{
      "name": "{name}",
      "npm": "@ai-sdk/openai-compatible",
      "options": {{
        "baseURL": "{base}",
        "apiKey": "{key}",
        "timeout": 600000
      }},
      "models": {{
        "{MODEL_ID}": {{
          "name": "{name}",
          "reasoning": true,
          "tool_call": true,
          "temperature": true,
          "attachment": true,
          "modalities": {{ "input": ["text", "image"], "output": ["text"] }},
          "interleaved": {{ "field": "reasoning_content" }},
          "options": {{ "reasoningEffort": "{effort}" }},
          "limit": {{ "context": {CONTEXT}, "output": {OUTPUT} }},
          "variants": {{
            "none": {{ "reasoningEffort": "none" }},
            "low":  {{ "reasoningEffort": "low" }},
            "max":  {{ "reasoningEffort": "max" }}
          }}
        }}
      }}
    }}'''


def opencode_apply(text: str, pid: str, block: str) -> str:
    """插入或替换 `"pid": { ... }` 块（按大括号配对定位，保留注释）。"""
    m = re.search(r'(?m)^(\s*)"%s"\s*:\s*\{' % re.escape(pid), text)
    if m:
        start = m.start()
        i = text.index("{", m.end() - 1)
        depth = 0
        in_str = False
        esc = False
        while i < len(text):
            c = text[i]
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
                        # 只替换到 '}' 为止：后面的逗号/换行原样保留（吃掉逗号会让下一个键缺分隔符）
                        end = i + 1
                        return text[:start] + m.group(1) + block.lstrip() + text[end:]
            i += 1
        raise SystemExit("[错误] opencode.jsonc 里 %s 块的括号不配对，请手工检查。" % pid)

    m = re.search(r'(?m)^(\s*)"provider"\s*:\s*\{\s*\n', text)
    if m:
        ins = m.end()
        return text[:ins] + block + ",\n" + text[ins:]

    # 没有 provider 段：新建整个文件/追加到根对象
    if not text.strip():
        return "{\n  \"provider\": {\n" + block + "\n  }\n}\n"
    idx = text.rstrip().rfind("}")
    if idx <= 0:
        return "{\n  \"provider\": {\n" + block + "\n  }\n}\n"
    head, tail = text[:idx], text[idx:]
    stripped = head.rstrip()
    comma = "" if stripped.endswith(("{", "[", ",")) else ","
    return stripped + comma + '\n  "provider": {\n' + block + "\n  }\n" + tail + "\n"


# ---- workbuddy（models.json）-----------------------------------------------
def workbuddy_entry(mid: str, name: str, base: str, key: str) -> dict:
    return {
        "id": mid,
        "name": name,
        "api": "openai-completions",
        "baseUrl": base,
        "apiKey": key,
        "contextWindow": CONTEXT,
        "maxTokens": OUTPUT,
        "reasoning": True,
        "input": ["text", "image"],
        "disabled": False,
        "description": "海南师范大学 DeepSeek（仅限本校师生本人使用）",
    }


def workbuddy_apply(text: str, entry: dict) -> str:
    if not text.strip():
        data = {"models": []}
    else:
        try:
            loaded = json.loads(text)
        except Exception:  # noqa: BLE001
            raise SystemExit("[错误] models.json 不是合法 JSON，请先修正或备份后删除。")
        # 顶层是数组时按「模型列表」处理（客户端会忽略纯数组，这里顺手升级为对象）
        data = {"models": loaded} if isinstance(loaded, list) else loaded
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    models = [m for m in models if not (isinstance(m, dict) and m.get("id") == entry["id"])]
    models.append(entry)
    data["models"] = models
    avail = data.get("availableModels")
    if isinstance(avail, list):
        if entry["id"] not in avail:
            avail.append(entry["id"])
        data["availableModels"] = avail
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---- dsh（settings.yaml 定点处理）------------------------------------------
def dsh_block(pid: str, name: str, base: str, mode: str) -> str:
    env_var = "HAINNU_API_KEY" if mode == "proxy" else "HAINNU_DIRECT_API_KEY"
    return (
        f"    {pid}:\n"
        f"      displayName: {name}\n"
        f"      apiKeyEnv: {env_var}\n"
        f"      api: openai-completions\n"
        f"      baseURL: {base}\n"
        f"      models:\n"
        f"        - id: {MODEL_ID}\n"
        f"          # 实测：学校 nginx 请求体上限 1MiB → 最坏密度下约 28.9 万 tokens，\n"
        f"          # 260000 为留 10% 余量后的取值（见 README「上下文长度取值」）。\n"
        f"          contextWindow: {CONTEXT}\n"
        f"          maxTokens: {OUTPUT}\n"
    )


def dsh_apply(text: str, pid: str, block: str) -> str:
    """在 llm-pi-ai.providers 下插入/替换 provider 块；缺失外层键时补齐。"""
    def indent_of(s: str, pat: str) -> int | None:
        m = re.search(pat, s)
        return len(m.group(1)) if m else None

    if not text.strip():
        return "llm-pi-ai:\n  providers:\n" + block

    if re.search(r"(?m)^\s*%s\s*:\s*$" % re.escape(pid), text):
        # 替换已有 provider（到下一个同级键或文件末尾为止）
        m = re.search(r"(?m)^(\s*)%s\s*:\s*(?:#.*)?$" % re.escape(pid), text)
        ind = len(m.group(1))
        start = m.start()
        lines = text.splitlines(keepends=True)
        pos = 0
        begin = None
        for i, ln in enumerate(lines):
            if pos == m.start():
                begin = i
                break
            pos += len(ln)
        end = len(lines)
        for j in range(begin + 1, len(lines)):
            ln = lines[j]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            cur = len(ln) - len(ln.lstrip(" "))
            if cur <= ind:
                end = j
                break
        # 保留块尾的空行：否则第二次执行会把它吃掉，导致内容每次都差一个换行（幂等被破坏）
        blanks = []
        k = end - 1
        while k > begin and not lines[k].strip():
            blanks.insert(0, lines[k])
            k -= 1
        return "".join(lines[:begin]) + block + "".join(blanks) + "".join(lines[end:])

    m = re.search(r"(?m)^(\s*)providers\s*:\s*(?:#.*)?$", text)
    if m and "llm-pi-ai" in text:
        ind = len(m.group(1))
        lines = text.splitlines(keepends=True)
        pos = 0
        for i, ln in enumerate(lines):
            if pos == m.start():
                # 插到 providers 的最后一个子键之后；没有子键就直接插在下一行
                j = i + 1
                child_indent = ind + 2
                last = i
                while j < len(lines):
                    ln2 = lines[j]
                    if ln2.strip() and not ln2.lstrip().startswith("#"):
                        cur = len(ln2) - len(ln2.lstrip(" "))
                        if cur <= ind:
                            break
                        if cur == child_indent:
                            last = j
                    j += 1
                # 找到 last 所在子块的结尾
                k = last + 1
                while k < len(lines):
                    ln2 = lines[k]
                    if ln2.strip() and not ln2.lstrip().startswith("#"):
                        cur = len(ln2) - len(ln2.lstrip(" "))
                        if cur <= ind:
                            break
                    k += 1
                return "".join(lines[:k]) + block + "".join(lines[k:])
            pos += len(ln)

    if "llm-pi-ai" in text:
        m = re.search(r"(?m)^(\s*)llm-pi-ai\s*:\s*(?:#.*)?$", text)
        ind = len(m.group(1))
        return (text.rstrip() + "\n"
                + " " * (ind + 2) + "providers:\n"
                + block)

    return text.rstrip() + "\n\nllm-pi-ai:\n  providers:\n" + block


def dsh_set_default(text: str, pid: str) -> str:
    """把 agent-default-model 指向刚配置的 provider/模型（否则 DSH 仍用旧模型名，会找不到）。"""
    block = "agent-default-model:\n  provider: %s\n  model: %s\n" % (pid, MODEL_ID)
    m = re.search(r"(?m)^agent-default-model\s*:\s*(?:#.*)?$", text)
    if not m:
        return text.rstrip() + "\n\n" + block
    lines = text.splitlines(keepends=True)
    pos, begin = 0, None
    for i, ln in enumerate(lines):
        if pos == m.start():
            begin = i
            break
        pos += len(ln)
    end = len(lines)
    for j in range(begin + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.lstrip().startswith("#") and (len(ln) - len(ln.lstrip(" "))) == 0:
            end = j
            break
    return "".join(lines[:begin]) + block + "".join(lines[end:])


def opencode_set_default(text: str, ref: str) -> str:
    """把顶层 "model" 指向刚配置的 provider/模型（opencode 也用它决定默认模型）。"""
    m = re.search(r'(?m)^(\s*)"model"\s*:\s*"[^"]*"', text)
    if m:
        return text[:m.start()] + '%s"model": "%s"' % (m.group(1), ref) + text[m.end():]
    m2 = re.search(r'(?m)^(\s*)"provider"\s*:\s*\{', text)
    if m2:
        ins = m2.start()
        return text[:ins] + '%s"model": "%s",\n' % (m2.group(1), ref) + text[ins:]
    idx = text.rstrip().rfind("}")
    if idx <= 0:
        return '{\n  "model": "%s",\n  "provider": {}\n}\n' % ref
    head, tail = text[:idx], text[idx:]
    stripped = head.rstrip()
    comma = "" if stripped.endswith(("{", "[", ",")) else ","
    return stripped + comma + '\n  "model": "%s"\n' % ref + tail + "\n"


# ---- zcode（~/.zcode/v2/config.json，纯 JSON 可整体读写）--------------------
def zcode_provider_block(name: str, base: str, key: str) -> dict:
    return {
        "name": name,
        # 学校端点只实现了 Chat Completions（/api/chat/completions），没有 OpenAI 的
        # Responses API → 必须用 openai-compatible（ZCode 对它拼接 /chat/completions）。
        # kind 填 "openai" 会被拼成 /responses，直接 404。
        "kind": "openai-compatible",
        "options": {"apiKey": key, "baseURL": base, "apiKeyRequired": True},
        "source": "custom",
        "enabled": True,
        "models": {
            MODEL_ID: {
                "limit": {"context": CONTEXT, "output": OUTPUT},
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "zcode": {"modalitiesConfigured": True, "modified": True},
            }
        },
        "zcode": {"deletedModels": []},
    }


def zcode_apply(text: str, name: str, base: str, key: str) -> str:
    """在 provider 下插入/更新海师供应商；重复执行只更新同一块，不堆积副本。

    ZCode 的这份配置是机器写的纯 JSON（无注释），整体反序列化回写是无损的，
    与 opencode/DSH 的「定点文本替换」约束不冲突。provider 键沿用 ZCode 自己
    生成自定义供应商时用的 UUID 风格；已有海师供应商按 baseURL + 模型识别。
    """
    data = json.loads(strip_jsonc(text)) if text.strip() else {}
    if not isinstance(data, dict):
        raise SystemExit("[错误] ZCode 配置顶层不是对象，请手工检查。")
    providers = data.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise SystemExit("[错误] ZCode 配置的 provider 段不是对象，请手工检查。")

    def is_ours(v: dict) -> bool:
        b = str((v.get("options") or {}).get("baseURL") or "")
        if "hainnu.edu.cn" not in b and b.rstrip("/") != base.rstrip("/"):
            return False
        return MODEL_ID in (v.get("models") or {})

    target = next((k for k, v in providers.items()
                   if isinstance(v, dict) and is_ours(v)), None)
    if target is None:
        target = str(uuid.uuid4())
    providers[target] = zcode_provider_block(name, base, key)
    data["provider"] = providers
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---- 前置条件（配置好 ≠ 能跑，这里把「还差什么」讲清楚）--------------------
def probe_local_service(port: int) -> bool:
    """探测本地代理是否在跑。注意：桥是单线程的，忙起来 /health 也不回 → 不通不代表没在跑。"""
    import urllib.request  # noqa: PLC0415

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def ask_yes_no(question: str, auto: bool) -> bool:
    """auto=True（--yes）时不提问，返回 False（只提示、不拉起程序）。"""
    if auto:
        return False
    try:
        return input(question).strip().lower() == "y"
    except EOFError:
        return False


def open_helper(bat: str) -> None:
    """拉起项目根目录里的某个 bat（新窗口，不阻塞本脚本）。"""
    p = BASE / bat
    if not p.exists():
        print("         （没找到 %s，请在项目根目录里手动双击它）" % bat)
        return
    try:
        os.startfile(str(p))  # Windows：用默认方式执行 bat
        print("         已打开：%s" % bat)
    except Exception as exc:  # noqa: BLE001
        print("         自动打开失败（%s），请手动双击 %s" % (exc, bat))


def check_prereqs(mode: str, port: int, auto: bool) -> bool:
    """打印前置条件并引导处理。返回 False = 需要先补齐条件，本次不再继续写配置。"""
    print("\n前置条件检查：")
    has_token = (BASE / "token.txt").exists()

    # ① 登录令牌（两条链路都要；直连还要把 JWT 写进客户端配置）
    if has_token:
        extra = "" if mode == "proxy" else " 将写入客户端配置。"
        print("  [ OK ] 登录令牌已就绪（token.txt）。" + extra)
    else:
        print("  [ 缺 ] 还没有登录令牌（token.txt 不存在）。")
        print("         → 获取方式二选一：")
        print("           · 双击项目根目录的「1.获取令牌.bat」，在弹出的浏览器里用校园账号登录；")
        print("           · 或打开图形管理台（start.bat / 启动管理台.bat）点「获取令牌」。")
        if ask_yes_no("\n  现在就打开「1.获取令牌.bat」吗？[y/N] ", auto):
            open_helper("1.获取令牌.bat")
            print("         登录完成后，token.txt 会自动生成。")
        if mode == "direct":
            print("\n  [中止] 直连必须把 JWT 写进客户端配置，没有令牌就无法配置。")
            print("         请先获取令牌，然后重新运行本脚本。")
            print("         （只想先配好、稍后再登录的话，可先选「经本地服务」的那条链路。）")
            return False
        print("         → 经本地服务的配置可以先写，但**用之前必须补上令牌**。")

    if mode != "proxy":
        return True

    # ② 本地代理是否在运行（仅经本地服务的链路需要）
    if probe_local_service(port):
        print("  [ OK ] 本地代理正在运行（127.0.0.1:%d）。" % port)
        return True

    print("  [ 缺 ] 本地代理没在运行 —— 这条链路的所有请求都要经过它。")
    print("         （若它其实开着、只是正在处理长请求，/health 也会没响应，属正常现象。）")
    print("         → 开启方式二选一：")
    print("           · 双击「2.启动代理.bat」，**保持窗口开着**（关窗口即停止）；")
    print("           · 或打开图形管理台点「启动代理」，还可在里面设开机自启。")
    if ask_yes_no("\n  现在就启动本地代理吗？[y/N] ", auto):
        open_helper("2.启动代理.bat")
        print("         稍等几秒看到 [ready] 即就绪；配置仍会继续写入。")
    else:
        print("         → 配置会照常写入，但**用之前请先启动代理**。")
    return True


# ---- 主流程 ----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--agent", required=True, choices=["opencode", "workbuddy", "dsh", "zcode"])
    ap.add_argument("--mode", required=True, choices=["proxy", "direct"])
    ap.add_argument("--config", default=None, help="手动指定配置文件路径（自动定位失败时用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的内容，不落盘")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--refresh-env", action="store_true",
                    help="（DSH 专用）只刷新密钥环境变量，不改动任何配置文件；令牌轮换后用")
    args = ap.parse_args()

    cfg = load_project_config()

    # 令牌轮换后的快捷刷新：不碰配置文件，只把最新密钥写回用户级环境变量。
    if args.refresh_env:
        if args.agent != "dsh":
            raise SystemExit("[错误] --refresh-env 目前仅支持 --agent dsh。")
        if args.mode == "proxy":
            var, value = "HAINNU_API_KEY", cfg.get("local_api_key") or "sk-hainnu"
        else:
            var, value = "HAINNU_DIRECT_API_KEY", load_jwt()
        ok, msg = set_user_env(var, value)
        if not ok:
            print("[失败] 环境变量写入未成功：%s\n  可手动执行：setx %s \"<密钥>\"" % (msg, var))
            return 1
        print("✓ 已刷新用户级环境变量 %s（长度 %d，值不回显）。" % (var, len(value)))
        print("  重开终端 / 重启 DSH 后生效。")
        return 0

    agent_label = {"opencode": "opencode", "workbuddy": "WorkBuddy",
                   "dsh": "DeepSeek-Harness (DSH)", "zcode": "ZCode"}[args.agent]
    mode_label = "经本地服务（127.0.0.1）" if args.mode == "proxy" else "直连学校"
    pid = "hainnu" if args.mode == "proxy" else "hainnu-direct"
    name = MODEL_NAME_PROXY if args.mode == "proxy" else MODEL_NAME_DIRECT

    print("=" * 62)
    print("  一键配置到 %s —— %s" % (agent_label, mode_label))
    print("=" * 62)

    # 前置条件（缺令牌时的引导）必须先跑：直连的 JWT 要在确认有令牌之后才去取
    if not check_prereqs(args.mode, int(cfg.get("port", 8787)), args.yes):
        return 1

    base, key = endpoints(cfg, args.mode)
    print("\n  Base URL : %s" % base)
    print("  模型     : %s（实际由服务端解析到上游真实 id）" % MODEL_ID)
    print("  上下文   : context %d / output %d" % (CONTEXT, OUTPUT))
    if args.mode == "direct":
        print("\n  ⚠️ 直连会把学校登录 JWT **明文**写进该客户端的配置文件，")
        print("     它等同于账号密码：仅限本人使用，不要外传这份配置。")
        if not args.yes:
            ok = input("\n  确认继续？[y/N] ").strip().lower()
            if ok != "y":
                print("已取消，未做任何改动。")
                return 0

    if args.agent == "zcode":
        print("\n  ⚠️ 若 ZCode 正在运行，请先**完全退出**再继续——")
        print("     它退出时可能回写配置文件，覆盖本次改动。")

    path = pick_config(args.agent, args.config)
    old = path.read_text(encoding="utf-8") if path.exists() else ""

    if args.agent == "opencode":
        ref = f"{pid}/{MODEL_ID}"
        new = opencode_apply(old, pid, opencode_block(pid, name, base, key, args.mode))
    elif args.agent == "workbuddy":
        ref = f"{pid}/{MODEL_ID}"
        new = workbuddy_apply(old, workbuddy_entry(ref, name, base, key))
    elif args.agent == "zcode":
        new = zcode_apply(old, name, base, key)
    else:
        ref = f"{pid}/{MODEL_ID}"
        new = dsh_apply(old, pid, dsh_block(pid, name, base, args.mode))

    # 是否顺带把它设为该客户端的默认模型（不设也能用，只是每次要手动切）
    if args.agent in ("workbuddy", "zcode"):
        pass  # WorkBuddy / ZCode 在界面里选模型，没有可写的「默认模型」字段
    elif args.yes or ask_yes_no("\n  同时把它设为 %s 的默认模型？[Y/n] " % agent_label, False):
        new = (opencode_set_default(new, ref) if args.agent == "opencode"
               else dsh_set_default(new, pid))

    apply_change(args.agent, path, new, args.dry_run)

    if args.dry_run:
        return 0

    print("\n✅ 已写入：%s" % path)
    print("\n⚠️ 配置写好了，但要真正跑起来还差下面几步（缺一不可）：")
    step = 1
    if args.mode == "proxy":
        if not (BASE / "token.txt").exists():
            print("  %d) 获取登录令牌：双击「1.获取令牌.bat」用校园账号登录一次。" % step)
            step += 1
        print("  %d) 启动本地代理：双击「2.启动代理.bat」并保持窗口开启。" % step)
        step += 1
    print("  %d) 重启 %s（配置只在启动时读取）。" % (step, agent_label))
    step += 1

    if args.agent == "dsh":
        var = "HAINNU_API_KEY" if args.mode == "proxy" else "HAINNU_DIRECT_API_KEY"
        print("\n  DSH 从环境变量读密钥，正在自动写入用户级环境变量：")
        ok, msg = set_user_env(var, key)
        if ok:
            shown = key if args.mode == "proxy" else "学校 JWT（长度 %d，值不回显）" % len(key)
            print("       ✓ %s = %s" % (var, shown))
            print("     已广播系统变更：重开终端 / 重启 DSH 即可读到；")
            print("     令牌轮换后重跑「更新令牌(DSH直连).bat」即可刷新，无需重新配置。")
        else:
            print("       [未成功] %s" % msg)
            print("       请手动设置后重开终端：setx %s \"<密钥>\"" % var)
    elif args.agent == "opencode":
        print("\n  验证：opencode models %s" % pid)
    elif args.agent == "zcode":
        print("\n  验证：重启 ZCode → 设置 → 模型供应商 里应出现「%s」，选择 %s。" % (name, MODEL_ID))
    else:
        print("\n  验证：在 WorkBuddy 的模型列表里选择「%s」。" % name)
    if args.mode == "proxy":
        print("\n  提示：经本地服务的链路需要本地代理正在运行（2.启动代理.bat）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
