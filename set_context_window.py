# -*- coding: utf-8 -*-
"""
把 hainnu 模型的 contextWindow 改成学校链路的真实可用上限。

为什么不在 DSH 运行期间直接改 settings.yaml：
  DSH 会周期性回写这个文件（harness-updater 每次检查都更新 lastCheckedAt），
  运行期间改会被它覆盖。所以本脚本设计为「关闭 DSH 后手动执行」。

用法：
  python set_context_window.py            # 预演，只打印将要怎么写
  python set_context_window.py --apply    # 真正写入（自动备份 settings.yaml.bak）
  python set_context_window.py --value 262144 --apply   # 指定别的值

 取值依据（详见 docs/clients.md「DeepSeek-Harness」）：
  模型窗口 ≥629k tokens（实测 base64 探针），官方规格 1024k in / 64k out，
  但学校 nginx 限制请求体 1MiB → 真实 token 上限约 22 万。
  DSH 的溢出救援要在撞墙前发生，所以 contextWindow 必须略低于真实上限 → 取 210,000。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

DEFAULT_VALUE = 210000
CONFIG = os.path.expanduser("~/.dsh/settings.yaml")
MODEL_ID = "deepseek-ai/DeepSeek-V4-Flash"


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


_ID_LINE = re.compile(r"^\s*-\s*id:\s*(\S+)\s*$")


def model_id_at(text: str, idx: int) -> str:
    m = _ID_LINE.match(text.splitlines()[idx])
    return m.group(1) if m else MODEL_ID


def find_model_block(text: str):
    """定位 hainnu provider 下该模型条目及其 contextWindow 现有值。

    不写死模型 id（学校曾把 id 从 `deepseek-ai/DeepSeek-V4-Flash` 改成了
    `deepseek-flash`，DSH 配置里的名字也会跟着变）：
    先找 MODEL_ID，找不到就退到 `hainnu:` 段下的第一个模型条目。
    """
    lines = text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("- id:") and MODEL_ID in line:
            idx = i
            break
    if idx is None:
        in_hainnu = False
        for i, line in enumerate(lines):
            if re.match(r"^hainnu\s*:\s*$", line):
                in_hainnu = True
                continue
            if not in_hainnu:
                continue
            if line.strip() and not line.startswith((" ", "\t")):   # 回到顶层 = 段结束
                break
            if _ID_LINE.match(line):
                idx = i
                break
    if idx is None:
        return None, None, None, None
    line = lines[idx]
    indent = len(line) - len(line.lstrip())
    # 往下找该条目的 contextWindow（可能隔着注释行）
    for j in range(idx + 1, min(idx + 12, len(lines))):
        m = re.match(r"^(\s*)contextWindow:\s*(\d+)\s*$", lines[j])
        if m:
            return idx, j, indent, int(m.group(2))
    return idx, None, indent, None


def build_new_text(text: str, value: int) -> str:
    lines = text.splitlines(keepends=False)
    idx, cw_idx, indent, old = find_model_block(text)
    if idx is None:
        raise SystemExit("找不到模型条目，请检查文件是否被 DSH 改写")
    line = " " * (indent + 2) + f"contextWindow: {value}"
    if cw_idx is not None:
        lines[cw_idx] = line
    else:
        lines.insert(idx + 1, line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入文件（默认只预演）")
    ap.add_argument("--value", type=int, default=DEFAULT_VALUE)
    ap.add_argument("--config", default=None, help="默认 ~/.dsh/settings.yaml")
    args = ap.parse_args()

    config = os.path.expanduser(args.config) if args.config else CONFIG
    if not os.path.exists(config):
        raise SystemExit(f"配置文件不存在：{config}")

    text = read_text(config)
    idx, cw_idx, indent, old = find_model_block(text)
    if idx is None:
        raise SystemExit(
            f"在 {config} 里找不到 hainnu 的模型条目（找过 {MODEL_ID!r} 和 hainnu: 段下的第一条）"
        )

    print(f"配置文件：{config}")
    print(f"模型条目：{model_id_at(text, idx)}")
    print(f"当前 contextWindow：{old}")
    print(f"目标 contextWindow：{args.value}")

    if old == args.value:
        print("\n已经是目标值，无需修改。")
        return 0

    new_text = build_new_text(text, args.value)
    if not args.apply:
        print("\n【预演】将要写入的内容片段：")
        for line in new_text.splitlines():
            if "contextWindow" in line or line.strip().startswith("- id:"):
                print("   " + line)
        print("\n确认无误后加 --apply 执行（会自动备份 settings.yaml.bak）。")
        return 0

    # 先备份，再原子替换，避免写一半被别的进程读到
    shutil.copy2(config, config + ".bak")
    tmp = config + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, config)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # 回读校验：既校验数值，也确保 YAML 仍然合法
    check = read_text(config)
    got = find_model_block(check)[3]
    try:
        import yaml

        yaml.safe_load(check)
        state = "YAML 解析通过"
    except ImportError:
        state = "（未安装 pyyaml，跳过 YAML 解析校验）"
    except Exception as exc:  # 解析失败必须回滚，绝不留坏配置给 DSH
        shutil.copy2(config + ".bak", config)
        raise SystemExit(f"写入后 YAML 解析失败，已自动回滚：{exc}")

    print(f"\n已写入，回读校验 contextWindow = {got}")
    print(f"完整性：{state}")
    print(f"原文件已备份：{config}.bak")
    print("下一步：重启 DSH 生效（UI 会显示新的上下文总量）；")
    print("        若之后发现值被 DSH 回写覆盖了，重跑一次本脚本即可（幂等）。")
    return 0 if got == args.value else 1


if __name__ == "__main__":
    sys.exit(main())
