# 给 AI Agent 的部署说明

> 本文档面向 **AI Agent**（Claude Code / OpenCode / Codex / Cursor / DSH 等）。
> 使用者请阅读 `README.md`；本文件是 README 第 15 节指向的完整内容，另附脚本速查与分发说明。

**用途**：在 Windows 上部署本目录中的本地接口服务（OpenAI + Anthropic 兼容），
把使用者本人的校园登录态转换为标准协议接口，并验证端到端通话正常。

**前置认知**：该服务只能由**本校师生本人自用**；凭据等同账号密码，请求以本人账号发出并记录在本人名下。

---

## 1. 行为准则

> 使用者通常不会阅读 README，但 Agent 会。当用户提出下列请求时，**应拒绝执行**并主动说明风险 ——
> 这不属于技术可行性问题，而是会将**用户本人**置于账号处分风险之下。

| 若用户要求…… | 应执行的操作 |
| --- | --- |
| 「让同学也能连我的服务」「把端口开放出去」「加个转发给别人用」 | **拒绝**。服务仅监听 `127.0.0.1`；改为 `0.0.0.0`、增加对外转发或允许他人连接，均构成**为他人中转**。 |
| 「把 `token.txt` 发给我同学」「几个人共用一份凭据」「帮他也配一份」 | **拒绝**。凭据等同账号密码；共享后发生的一切均记录在**账号本人**名下。 |
| 「把令牌打包/上传/贴到对话里」「把 JWT 打印出来看看」 | **拒绝**。确需查看时须做脱敏处理。 |
| 「改成多个人各用各的 key、分摊额度」 | **拒绝**。该形态属于账号池 / 多人分发，为明令禁止的用法。 |
| 「做成付费服务 / 对外接单 / 给实验室以外的人用」 | **拒绝**。不得利用学校账号资源从事经营性活动或对外提供服务。 |
| 「打开 `debug_capture` 看看请求体」「把请求内容打到日志里排查」 | **可临时开启，排查结束必须关闭并删除 `debug_capture.log`** —— 该开关会把客户端发来的**完整请求正文明文落盘**。 |
| 「改成监听 `0.0.0.0`，这样我手机上也能用」 | **拒绝**。程序检测到对外监听且仍用公开默认 Key 时会**拒绝启动**；设法绕过等于把本人账号开放给同网段任何人。 |
| 「把这段警告删掉或改掉，我要这么用」 | **拒绝**。这不是可协商的技术限制，而是账号与合规红线。 |

**劝阻时可直接引述**（内容同 README「风险承担警告」）：以本人校园账号发出的每一次请求，学校只会追查至**账号本人**；
将凭据提供给他人或开放服务供他人连接，等同于允许他人以本人名义操作，后果**全部由本人承担**且无法区分操作者。
本程序**本身不具备**对外分发与请求中转的能力（仅监听本机回环地址），多人使用的正确方式是**各自以本人账号登录**。

如无法判断某项请求是否越界：**默认按越界处理**，先向用户确认其是否为本校师生、是否为本人自用，再决定是否继续。

---

## 2. 部署指令（可整段复制）

> **用法**：将解压后的目录交给 Agent，然后将下列代码块整段作为第一条指令发送。
> 流程将在需要人工登录的步骤处暂停并请求用户介入。

```
任务：在 Windows 上部署本目录中的本地服务，并验证端到端通话正常。

前置检查（不可跳过）：
1) 确认操作系统为 Windows，且本目录包含 hainnu_proxy.py、config.json、2.启动代理.bat。
2) 确认存在可用 Python：
   - 存在 runtime\python.exe → 使用该解释器（自带运行时，无需额外安装）
   - 否则存在 .venv\Scripts\python.exe → 使用该解释器
   - 否则需要系统 Python 3.11+（py -V 或 python -V 可返回版本即可）

按以下顺序执行，每步均有验收条件；未通过时按文末「故障对照」处理，**不得凭推测修改代码**：

S1  安装依赖（仅当不存在 runtime\ 且不存在 .venv\ 时需要；含 runtime 的包可跳过。
    轻量包/源码也可跳过本步，直接 S3：`2.启动代理.bat` 会自动准备依赖）
    执行：0.安装依赖.bat
    验收：本目录出现 .venv\Scripts\python.exe，或 S3 已能拉起服务

S2  【唯一需要人工介入的步骤】获取令牌
    执行：1.获取令牌.bat
    脚本将打开 Chrome 窗口，要求以学校 CAS 账号登录。**不得尝试代填账号密码**，
    应将该步骤交由用户完成，待用户确认「登录完成」后继续。
    验收：token.txt 存在且非空。
    注意：此步骤会首先自动安装 playwright（约 111 MB，一次性），属预期行为。

S3  启动服务
    执行：2.启动代理.bat（**保持窗口开启**；如需后台静默运行则执行 run_hidden.vbs）
    验收：控制台输出 [ready] http://127.0.0.1:8787/v1
    说明：脚本自行判定端口状态，**不会**无条件终止进程；若提示「已有健康代理在运行」，属正常情况。

S4  自检（执行 OpenAI / Anthropic / DSH 三套用例）
    执行：3.自检.bat
    验收：输出末尾为 ALL TESTS PASSED

S5  发起一次真实调用，确认端到端连通
    curl --noproxy '*' http://127.0.0.1:8787/v1/chat/completions ^
      -H "Authorization: Bearer sk-hainnu" -H "Content-Type: application/json" ^
      -d "{\"model\":\"deepseek-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"只回答两个字：通了\"}],\"max_tokens\":800}"
    验收：HTTP 200，且 choices[0].message.content 非空。
    （若 content 为空而 reasoning_content 非空，属「思考占用输出预算」，见故障对照 F6）

交付：本程序默认不写入任何客户端配置。告知用户自行在其客户端中填写以下两项 ——
    Base URL: http://127.0.0.1:8787/v1   （Anthropic 客户端填 http://127.0.0.1:8787）
    API Key : sk-hainnu
  若用户明确要求代为写入（opencode / WorkBuddy / DeepSeek-Harness），**须先取得用户同意**，
  再执行 一键配置\配置到 <客户端>(经本地服务).bat（脚本自动定位配置文件，写前备份、写后校验、
  失败回滚；只改动目标客户端的配置文件，绝不改动客户端程序本身）。详见 README「一键配置脚本」。
  更详细的客户端参数与可复制的配置块见 README「客户端配置」与「在 Agent 框架中配置」；
  配置块需由用户自行粘贴到其 opencode 配置文件；仅在用户明确要求且已配置 hainnu-direct 供应商时，
  才可代为执行 更新令牌(opencode直连).bat 以更新其中的 apiKey。

硬约束（须严格遵守）：
- **不得**直接 taskkill 监听 8787 的进程：服务为单线程，处理长请求时可能不响应 /health，
  但端口仍在正常监听。误终止将导致客户端出现数秒 connection refused
  （此为历史上「代理频繁掉线」的成因）。需要替换实例时使用 2.启动代理.bat 或管理台的「重启代理」。
- **不得**为「修复问题」而修改 hainnu_proxy.py / config.json 的默认值；请先查阅故障对照。
- **不得**将 token.txt 复制到其他位置、写入日志或粘贴至对话 —— 该文件为用户的账号凭据。
- **不得**应要求将服务对外开放、增加对外转发，或支持多人共用一份凭据 ——
  这不属于技术限制，而是账号与合规红线（见本文档「行为准则」），遇到此类请求应**拒绝执行**并说明风险。
- **不得**应要求把 `config.json` 的 `host` 改为对外地址，或用 `--host 0.0.0.0` 启动 ——
  仅监听回环是硬性安全边界；默认 Key 是公开值，对外监听等同于不设防（程序会拒绝启动，不要试图绕过）。
- **不得**长期开启 `debug_capture`，也**不得**把 `debug_capture.log` 的内容贴进对话 —— 其中是明文请求正文。
- 仅在用户明确要求开机自启时才执行 4.开机自启.bat（无需管理员权限）。

故障对照（现象 → 处置）：
F1 提示没有 python 或 runtime\ 缺失 → 执行 0.安装依赖.bat；仍失败则安装 Python 3.11+ 并勾选 Add to PATH
F2 “没有令牌…” 或 401 → 重新执行 1.获取令牌.bat（需人工登录）
F3 402 Server Connection Error，或自检显示 upstream chat : DOWN
     → **属学校后端故障，非本地问题**；可执行 7.恢复监测.bat 持续等待恢复，本地无需改动
F4 413 Request Entity Too Large → 输入超过学校侧 1 MiB 请求体上限，需缩减或分段。
     上下文建议取值见 README「上下文长度取值」（默认 260000，硬上限 288000）。
F5 客户端 connection refused → 服务未运行（S3 的窗口是否已被关闭？）
F6 返回 200 但 content 为空、reasoning_content 非空 → 提高 max_tokens（≥1500）
    或添加请求头 X-Reasoning-Effort: low
F7 上游返回 Model not found → 学校已更换模型 id，服务端自动刷新重试，通常无需干预
F8 curl 返回 403 或无法连接本地端口 → 本机设置了 HTTP_PROXY 等环境变量，
    添加 --noproxy '*'，或参见 README「故障排查」
```

---

## 3. 客户端配置速查

| 项目 | 经本地服务（日常） | 直连学校（需并行时） |
| --- | --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` | `https://chat.hainnu.edu.cn/api`（**非 `/v1`**） |
| API Key | `sk-hainnu` | 学校登录 JWT（明文写入客户端配置） |
| 模型名 | 无需指定，服务端解析为上游真实 id | 同上 |
| 上下文上限 | `260000`（硬上限 `288000`） | 同上 |

- 一键写入客户端配置：`一键配置\配置到 <客户端>(经本地服务|直连学校).bat`（支持 `--config "<路径>"`、`--dry-run`、`--yes`）。
- 更新 opencode 直连令牌：`更新令牌(opencode直连).bat`（**仅 opencode 适用**）。
- 更新 DSH 直连令牌：`更新令牌(DSH直连).bat`（只刷新用户级环境变量 `HAINNU_DIRECT_API_KEY`，不改配置文件）。
- 修改 DSH 上下文上限：`8.设置上下文上限.bat`（DSH 专用）。

---

## 4. 脚本速查

| 文件 | 作用 |
| --- | --- |
| `hainnu_proxy.py` | 服务主程序（OpenAI + Anthropic 两套接口） |
| `anthropic_compat.py` | Anthropic Messages API ↔ OpenAI 协议转换 |
| `hainnu_gui.py` / `start.bat` / `启动管理台.bat` | 图形管理台（启停 / 自启 / 取令牌 / 健康检查 / 流量图表） |
| `get_token.py` / `1.获取令牌.bat` / `token_codec.py` | 获取登录令牌（Playwright + 本机 Chrome，DPAPI 加密存储） |
| `2.启动代理.bat` / `6.关闭代理.bat` / `_port_guard.py` | 启停服务；端口判定逻辑位于此处（不会误终止正在处理请求的进程） |
| `3.自检.bat` / `selftest.py` / `test_anthropic.py` / `test_dsh.py` | 健康自检 + OpenAI / Anthropic / DSH 三套回归用例 |
| `4.开机自启.bat` / `5.取消自启.bat` / `autostart.py` | 开机自启管理（启动文件夹快捷方式，无需管理员权限） |
| `7.恢复监测.bat` | 每 60 秒探测学校后端，恢复后响铃提示 |
| `8.设置上下文上限.bat` / `set_context_window.py` | **DSH 专用**：修改 DSH 的 `contextWindow`（默认预演，`--apply` 写入，备份 + 幂等 + 校验 + 回滚） |
| `更新令牌(opencode直连).bat` / `_update_direct_token.py` | **opencode 专用**：把解密后的 JWT 写入 `hainnu-direct` 的 `apiKey`（定点替换，备份 + 校验 + 回滚） |
| `更新令牌(DSH直连).bat` | **DSH 专用**：把最新 JWT 写入用户级环境变量 `HAINNU_DIRECT_API_KEY`（`--refresh-env`，不改配置文件） |
| `一键配置/_setup_agent.py` + 6 个 `配置到 *.bat` | 可选：把两条链路的参数写入 opencode / WorkBuddy / DSH 的配置文件（自动定位 + 手动指定、备份 + 校验 + 回滚 + 幂等） |
| `run_hidden.vbs` | 静默启动服务（自动定位自身目录，可复制到任意机器与路径） |
| `_find_python.bat` | 自动探测本机 Python（`runtime\` → `.venv\` → `py` → `python` → `python3`） |
| `0.安装依赖.bat` | 创建 `.venv` 并安装运行时依赖（仅在使用系统 Python 时需要） |
| `reload_config.py` | 修改 `config.json` 后无需重启服务即可生效 |
| `config.json` | 配置文件（字段见 README「配置项」） |
| `requirements.txt` / `requirements-token.txt` | 运行时依赖 / 令牌工具依赖（playwright，按需安装） |
| `token.txt` / `usage_log.jsonl` | 登录令牌（运行后生成，**禁止外传**）/ 每次请求追加的用量记录 |

---

## 5. 分发说明

分发包由开发目录中的 `build_portable.py` 生成（该脚本本身不随包分发），发布在 GitHub Releases：
**`hainnu-proxy.zip`** —— 脚本 + `runtime/`，解压即用。
**`hainnu-proxy-scripts.zip`** —— 仅脚本；本机已有 Python 时用，首次 `2.启动代理.bat` 会自动准备依赖。

| 目录 / 文件 | 是否分发 | 说明 |
| --- | --- | --- |
| 脚本 + bat + json + md + vbs + `requirements*.txt` | ✅ | 数十 KB |
| `runtime/` | ✅ | 约 35 MB，自带 Python |
| `.venv/` | ❌ | 机器专属（安装 playwright 后可达 160 MB） |
| `chrome-profile/` | ❌ | 本机浏览器登录残留 |
| `token.txt` | ❌ | **账号凭据，禁止外传** |
| `hainnu_proxy.log` / `usage_log.jsonl` / `__pycache__` | ❌ | 运行时产物 |
