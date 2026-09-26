"""打包 hainnu-proxy。

只打「功能本体」：启动/运维脚本 + 核心代码 + 自检 + 文档配置。
**实验与测试产生的、或只服务实验测试的东西一律不进包**（探测/压测/对照脚本、离线测试、
日志、临时文件、备份、以及为某个外部工具做的集成小工具）。

只产出一个 zip：`hainnu-proxy.zip` —— 脚本 + `runtime/` 便携 Python，**解压即用**
（用户要求：包名就叫项目名，不要"分享版/含运行时"之类的后缀；
也不再提供「仅脚本」瘦身版 —— 完整包是唯一的发布物）。

三层防线（任意一层出问题都会中止，不会产出半成品）：
  1. **白名单**：只有 FILES 里列出的文件才进包（加 runtime/）
  2. **黑名单兜底**：名字命中 FORBIDDEN_* 的一律拦下（防手滑加进白名单）
  3. **内容扫描**：把包内所有可读文本解出来，扫个人标识（用户名/真名/私人目录/会话 id/令牌），
     以及**校验每个 bat/vbs 引用的脚本都在包里** —— 防止"看着打好了其实少文件"

用法：
    python build_portable.py            # 全部打包
    python build_portable.py --scripts  # 只打脚本包（快）
"""
from __future__ import annotations

import io
import re
import sys
import time
import zipfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent

# ---- 进包的文件（功能本体；改动前先想清楚"使用者真的需要它吗"）--------------
FILES = [
    # 启动与运维
    "0.安装依赖.bat",
    "1.获取令牌.bat",
    "2.启动代理.bat",
    "3.自检.bat",
    "4.开机自启.bat",
    "5.取消自启.bat",
    "6.关闭代理.bat",
    "7.恢复监测.bat",
    "8.设置上下文上限.bat",
    "更新令牌(opencode直连).bat",    # 直连链路的令牌维护（README §8.5），属功能件
    "更新令牌(DSH直连).bat",         # DSH 直连：刷新 HAINNU_DIRECT_API_KEY（README §8.5）
    "启动管理台.bat",
    "_find_python.bat",          # 上面几乎每个 bat 都要调它
    "start.bat",
    "run_hidden.vbs",
    # 核心代码
    "hainnu_proxy.py",
    "hainnu_gui.py",
    "app.ico",                   # 窗口/任务栏图标（DeepSeek 鲸鱼）
    "deepseek.png",              # 用量表 DeepSeek 模型图标
    "_port_guard.py",            # 2.启动代理.bat / 6.关闭代理.bat 的依赖（虽然带下划线，但是功能件）
    "_deps_check.py",            # 2.启动代理.bat / 管理台启动代理前的依赖自检与自动安装；只用标准库，缺依赖时也能跑
    "anthropic_compat.py",
    "autostart.py",
    "get_token.py",
    "token_codec.py",
    "set_context_window.py",
    "reload_config.py",
    "_update_direct_token.py",   # 更新令牌(opencode直连).bat 的依赖
    # 一键配置（把接口写进 Agent 客户端的配置：统一核心 + 6 个入口，见 README §8.6）
    "一键配置/_setup_agent.py",
    "一键配置/配置到 opencode(经本地服务).bat",
    "一键配置/配置到 opencode(直连学校).bat",
    "一键配置/配置到 WorkBuddy(经本地服务).bat",
    "一键配置/配置到 WorkBuddy(直连学校).bat",
    "一键配置/配置到 DeepSeek-Harness(经本地服务).bat",
    "一键配置/配置到 DeepSeek-Harness(直连学校).bat",
    # 自检（3.自检.bat 逐个调用，属功能）
    "selftest.py",
    "test_anthropic.py",
    "test_dsh.py",
    # 文档与配置
    "README.md",
    "AGENT.md",   # 给 AI Agent 的部署说明；README §14 指向此文件
    "config.json",
    "requirements.txt",
    "requirements-token.txt",
]

# ⚠️ 判据（用户明确）：**只打「使用者需要的功能」**（脚本 / 代码 / 文档 / 配置）。
#    凡是「我们做实验、测试产生的」或「只服务实验测试的」一律不进包 —— 包括提示词实验记录
#    （`注入提示词.md`、`并发推理提示词.md`，那是她自己的 A/B 实测笔记，README 也不引用它们）。

# 不进包的：实验/测试产物、日志、隐私、备份、外部工具集成件
# （白名单已经挡住了，这里是**兜底**：万一以后有人手滑加进 FILES，这一层会拦住）
FORBIDDEN_NAMES = (
    "token.txt",            # 登录凭据（换机也用不了，必须使用者自己生成）
    "usage_log.jsonl",      # 使用记录
    "savings_state.json",   # 省钱统计状态
    "user_name.txt",        # 真实姓名缓存 ← 分享包必须去掉
    "opencode.jsonc",
    # 提示词实验记录（她自己的 A/B 实测笔记，不是功能）
    "注入提示词.md",
    "并发推理提示词.md",
)
FORBIDDEN_TOPDIR = (".venv", "chrome-profile", ".ruff_cache", "__pycache__")
FORBIDDEN_SUFFIX = (".log", ".tmp")
FORBIDDEN_INFIX = (".bak",)     # 任何备份文件（*.bak、*.bak-<时间戳>）

# 包内文本里出现这些就算泄漏 → 中止。
# ⚠️ 这里**只放格式型规则**（不含个人信息）。个性化关键词（本机用户名、真名、私人目录、
#    压测隔离工作区等）一律写进同目录的 `private_patterns.txt`，每行一个，**该文件不提交**
#    （已列入 .gitignore），脚本启动时自动加载 —— 这样本脚本本身可以公开而不泄露隐私。
PRIVATE_PATTERNS: tuple = ()

# 同上，但要用正则精确匹配（防误伤：Python 标准库里到处是 classes_/bases_ 这种词）
PRIVATE_REGEXES = (
    # opencode 会话 id：`ses_` 前缀 + 10 位以上字母数字（此处不放真实例，避免把实际会话 id 写进仓库）
    r"ses_[0-9A-Za-z]{10,}",
    # 完整形状的 JWT（三段、每段 ≥8 字符）。⚠️ 不能只匹配裸 "eyJ" ——
    # get_token.py 里就有合法的 `startswith("eyJ")` 格式判断，会被误伤。
    # README 现在带 apiKey 字段，真要防的是"把真令牌粘进文档/配置"。
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
)

# 这些名字允许"包里有引用但包里没有"（运行时由使用者自己产生 / 旧注释里提到的历史名字）
ALLOW_MISSING = {
    "token.txt", "opencode.jsonc", "python.exe", "pythonw.exe",
    "1.get-token.bat", "2.start-proxy.bat", "0.install-deps.bat",   # 0 号 bat 注释里的旧英文名
    "hainnu_deps.txt",       # 2.启动代理.bat 写到 %TEMP% 的临时文件，运行时自己产生
}
TEXT_EXT = (".py", ".bat", ".vbs", ".ps1", ".md", ".txt", ".json", ".cfg", ".ini", ".yaml", ".yml")


def is_forbidden(rel: str) -> bool:
    """rel 是包内相对路径（posix 风格）。"""
    parts = rel.split("/")
    name = parts[-1]
    if name in FORBIDDEN_NAMES:
        return True
    if parts[0] in FORBIDDEN_TOPDIR:
        return True
    if name.lower().endswith(FORBIDDEN_SUFFIX):
        return True
    if any(b in name for b in FORBIDDEN_INFIX):
        return True
    # runtime/ 是便携 Python 自带目录，里面的 __pycache__ 必须保留（否则首次启动极慢）
    if parts[0] == "runtime":
        return False
    return "__pycache__" in parts


def add_runtime(zf: zipfile.ZipFile) -> int:
    """把 runtime/ 便携 Python 加进包。

    注意：**必须保留 __pycache__**。试过剔除它，包从 37MB 降到 28MB，
    但解压后首次启动要现场编译整个标准库，8 秒都起不来——使用者会以为坏了。
    便携版讲究解压即用，宁可大 9MB 也要秒开（实测）。
    """
    rt = BASE / "runtime"
    n = 0
    for p in rt.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(BASE).as_posix()
        zf.write(p, rel, zipfile.ZIP_DEFLATED)
        n += 1
    return n


def read_text_any(data: bytes) -> str:
    """包内文本可能是 UTF-8 也可能是 GBK（bat 都是 GBK）——两种都试。"""
    out = []
    for enc in ("utf-8", "gbk"):
        try:
            out.append(data.decode(enc))
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(out)


def load_private_patterns() -> tuple:
    """从 `private_patterns.txt` 读个性化关键词（每行一个，支持正则，`#` 开头为注释）。

    该文件**不提交到仓库**（见 .gitignore），用于放本机用户名、真名、私人目录名等
    不便写进公开代码的内容。文件不存在时返回空（扫描仅用内置的格式型规则）。
    """
    f = BASE / "private_patterns.txt"
    if not f.exists():
        return ()
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return tuple(out)


def scan_zip(zf: zipfile.ZipFile) -> list[str]:
    """对包内每个文件做内容扫描，返回问题列表（空 = 干净）。"""
    problems: list[str] = []
    pats = tuple(PRIVATE_PATTERNS) + load_private_patterns()
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if not name.lower().endswith(TEXT_EXT) or info.file_size > 3_000_000:
            continue
        try:
            txt = read_text_any(zf.read(name))
        except Exception:  # noqa: BLE001
            continue
        for pat in pats:
            if pat in txt:
                problems.append(f"{name} 含个人标识 {pat!r}")
        for rx in PRIVATE_REGEXES:
            m = re.search(rx, txt)
            if m:
                problems.append(f"{name} 含个人标识 {m.group(0)[:24]!r}")
    return problems


def check_references(names: list[str]) -> list[str]:
    """校验 bat/vbs 里引用的脚本都在包里（防止漏打依赖）。返回缺失清单。

    引用按「脚本所在目录优先」解析：`一键配置/` 下的 bat 里写的 `_setup_agent.py`
    指的是 `一键配置/_setup_agent.py`（bat 用 %~dp0 定位，等价于同目录），
    根目录脚本则按根目录解析 —— 不能只拿全路径比对，否则子目录里的脚本全被误报。
    """
    have = set(names)
    root_names = {n for n in names if "/" not in n}
    base_names = {n.rsplit("/", 1)[-1] for n in names}

    def present(entry: str, ref: str) -> bool:
        if ref in have:
            return True
        d = entry.rsplit("/", 1)[0] if "/" in entry else ""
        if d and (d + "/" + ref) in have:
            return True
        if ref in root_names:
            return True
        # 裸文件名：bat 里写 "一键配置/_setup_agent.py" 时正则只截到 "_setup_agent.py"，
        # 用「包内是否存在同名文件」兜底（当前包内脚本名唯一，不会误判）。
        return ref in base_names

    miss: set[str] = set()
    # 扩展名要列全（含 jsonc）：否则 "opencode.jsonc" 会被截成 "opencode.json"，
    # 而 ALLOW_MISSING 里只有 jsonc → 误报「引用了包里没有的文件」。
    pat = re.compile(r"[\w\u4e00-\u9fff\.\-\(\)]+\.(?:py|bat|vbs|ps1|jsonc|json|txt|md)")
    for entry in names:
        low = entry.lower()
        if not low.endswith((".bat", ".vbs", ".ps1")):
            continue
        # 只查包根目录下我们自己的脚本；runtime/ 是便携 Python 自带的一堆脚本，
        # 它们互相用相对路径引用（如 Activate.ps1 自己引用自己），不在本次校验范围内。
        if entry.startswith("runtime/"):
            continue
        raw = (BASE / entry).read_bytes()
        txt = read_text_any(raw)
        # bat 里写 "%~dp0xxx.py"，%~dp0 会被正则当成 "dp0xxx.py" 的一部分 → 先抹掉
        txt = txt.replace("%~dp0", "").replace("%~dp0", "")
        # Windows 路径分隔符统一成 "/"，否则 "一键配置\_setup_agent.py" 会被正则从
        # 反斜杠后开始匹配，只剩 "_setup_agent.py" → 误报「引用了包里没有的文件」。
        txt = txt.replace("\\", "/")
        for m in pat.findall(txt):
            m = m.lstrip("(")
            # 带子目录的引用按「包内相对路径」解析（bat 里写 "一键配置/_setup_agent.py"）
            if m in ALLOW_MISSING or present(entry, m) or present(entry, m.split("/")[-1]):
                continue
            miss.add(f"{entry} -> {m}")
    return sorted(miss)


def build(out_name: str, with_runtime: bool) -> Path:
    out = BASE / out_name
    if out.exists():
        bak = out.with_suffix(out.suffix + f".bak-{time.strftime('%Y%m%d')}")
        out.replace(bak)
        print(f"  已备份旧包 -> {bak.name}")

    missing = [f for f in FILES if not (BASE / f).exists()]
    if missing:
        print("  [警告] 以下文件不存在，已跳过：", missing)

    # 先写临时包，校验通过后再替换正式包：
    # 这样即使校验失败也不会破坏已有成品，且不必调用删除（沙箱里删除常被拦）。
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in FILES:
            p = BASE / name
            if p.exists():
                zf.write(p, name, zipfile.ZIP_DEFLATED)
        n_rt = add_runtime(zf) if with_runtime else 0

    # ---------------- 校验 ----------------
    with zipfile.ZipFile(tmp) as zf:
        names = zf.namelist()
        # 1) 黑名单兜底
        leaks = [n for n in names if is_forbidden(n)]
        if leaks:
            raise SystemExit(f"[中止] 包里混入了不该有的文件（{leaks[:5]}），"
                             f"临时包保留在 {tmp.name} 供检查")
        # 2) 压缩完整性
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"[中止] 压缩包损坏：{bad}")
        # 3) 关键文件必须在
        must = ("hainnu_proxy.py", "hainnu_gui.py", "config.json",
                "1.获取令牌.bat", "_port_guard.py")
        lack = [m for m in must if m not in names]
        if lack:
            raise SystemExit(f"[中止] 缺少关键文件：{lack}")
        # 4) 不能有实验/测试产物漏进来（白名单之外的非 runtime 文件）
        extra = [n for n in names
                 if not n.startswith("runtime/") and n not in FILES]
        if extra:
            raise SystemExit(f"[中止] 出现白名单之外的文件：{extra[:5]}")
        # 5) 内容扫描（个人标识）
        problems = scan_zip(zf)
        if problems:
            raise SystemExit("[中止] 内容里发现个人信息：\n  - " + "\n  - ".join(problems[:10])
                             + f"\n（临时包保留在 {tmp.name} 供检查）")
        # 6) bat 引用完整性
        refs_miss = check_references(names)
        if refs_miss:
            raise SystemExit("[中止] 有脚本引用了包里没有的文件：\n  - "
                             + "\n  - ".join(refs_miss[:10])
                             + f"\n（临时包保留在 {tmp.name} 供检查）")

    tmp.replace(out)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"  完成：{out.name}  {len(names)} 项  {size_mb:.1f} MB"
          + (f"（含 runtime {n_rt} 文件）" if with_runtime else ""))
    return out


def main() -> int:
    args = sys.argv[1:]
    print("=" * 62)
    print("打包 hainnu-proxy（不含实验/测试产物与个人信息）")
    print("=" * 62)

    if "--scripts" in args:
        # 轻量包：不含便携运行时。能成立是因为启动脚本会先跑 _deps_check.py，
        # 本机有现成的依赖就复用，没有才建 .venv 装上 —— 因此不需要打包 38MB 运行时。
        print("\n[不含 runtime —— 首次启动会自动准备依赖]")
        out = build("hainnu-proxy-scripts.zip", with_runtime=False)
    else:
        print("\n[含 runtime，解压即用]")
        out = build("hainnu-proxy.zip", with_runtime=True)

    print(f"\n全部完成 ✅  → {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
