# Hainnu Proxy · 校园大模型本地接口服务

> ## 使用资格
>
> 本项目**仅限海南师范大学本校师生使用**：须持有该校校园账号，并能通过学校统一身份认证（CAS）登录。
> 无校园账号者无法获取登录凭据，服务亦无法工作。
>
> ## 使用约束（约束对象为**凭据与请求中转**，不含代码）
>
> 1. **禁止外传账号凭据** —— `token.txt` 与 JWT 等同于账号密码，仅限本人使用，
>    不得以任何形式发送给他人（含同学），亦不得上传至任何平台或代码仓库。
> 2. **禁止为他人中转请求** —— 服务仅监听 `127.0.0.1`，仅服务于本机客户端；
>    不得对外开放端口、允许他人连接、或代他人发起请求。
> 3. **禁止共用凭据** —— 每位使用者应以本人校园账号登录，不得多人共用一份凭据。
>
> 代码本身以 MIT 许可证公开，可自由获取、学习、改造；用于其他单位的 Open WebUI 实例时，
> 需自行调整令牌获取流程中的登录页判定（见 §1 末段）。

[使用教程](README.md) · [给 AI Agent](AGENT.md) · [安全策略](SECURITY.md) ·
[贡献指南](CONTRIBUTING.md) · [行为准则](CODE_OF_CONDUCT.md)

**请从 [Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest) 下载最新包。**

- `hainnu-proxy.zip`：含便携 `runtime\`，解压即用。
- `hainnu-proxy-scripts.zip`：仅脚本，本机已有 Python 即可；第一次启动会自动安装依赖。

不要把仓库页面的「Code → Download ZIP」当成便携安装包（那里没有 `runtime\`）。若用源码，直接执行 `2.启动代理.bat`，缺依赖时会自动安装。

## 0. 声明

**本库为纯粹vibe coding产物。**

**任何问题/想给我加学分，请联系 sapphirestar46@outlook.com**

## 1. 概述

本程序在本机运行一个兼容 **OpenAI API** 与 **Anthropic Messages API** 的本地接口服务，把学校已部署的
大模型（DeepSeek）以标准协议的形式暴露在 `127.0.0.1` 上。

**功能边界**：只做「协议与凭据」这一层 —— 提供接口、附加登录凭据、转换协议、记账与自我保护。
**不负责把模型装进任何客户端或 Agent 框架**：客户端是否支持这两种协议、如何填写 Base URL 与 Key，
取决于客户端自身；本程序仅保证接口按协议返回。

- **权限与本人一致**：以本人账号与登录态发出请求，可用范围与登录学校网页相同，不产生额外权限。
- **凭据本地加密存储**：不上传、不外传。
- **仅对已有接口做规范化**：补充登录凭据、归一化字段，并额外提供 Anthropic 协议支持。

默认面向 `https://chat.hainnu.edu.cn/`（海南师范大学）；换站点时修改 `config.json` 的 `upstream`，
但 `get_token.py` 的登录判定需按目标站点调整。

> **术语约定**：下文中 **本地服务** 指本程序进程（即 `hainnu_proxy.py`）；**上游** 指学校 Open WebUI 服务。

---

## 2. 特性

| 项目 | 说明 |
| --- | --- |
| 双协议 | 同时提供 OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages`；完整工具调用回合已实测通过 |
| 模型名跟随上游 | 不预置映射表，实时拉取 `/api/models`；上游更换模型 id 后自动适配，`Model not found` 后刷新重试。无法匹配的模型名回退到上游可用模型（§7.1） |
| 单次登录 | 凭据经 Windows DPAPI 加密落盘；客户端侧仅填本地 key，无需接触 JWT |
| 协议归一化 | 自动翻译 DSH 的 `developer` 角色、`max_completion_tokens` 等字段为上游可识别形式 |
| 限流保护 | 命中上游限流后自动冷却并按指数退避错峰重试（上游规则：临时停用，通常 1 分钟内自行恢复） |
| 错误透传 | 上游 401 / 402 / 413 原样返回，不封装为 200 + 空正文 |
| 用量计量 | 每次请求追加一行 `usage_log.jsonl`（含模型、推理等级、缓存命中、首字/总耗时等字段）；管理台提供流量曲线与用量表，并给出缓存命中率与费用估算 |
| 图形管理台 | 启停、重启、开机自启、获取令牌、健康自检；历史流量支持曲线 / 表格切换，时间窗可选 1 小时 / 24 小时 / 1 周 / 1 月 |
| 官方价与省钱 | 每次启动管理台时自动获取 DeepSeek 官方价（官方定价页，失败回退 OpenRouter），按**实测缓存命中**折算等额费用与累计节省 |
| 进程安全 | 服务为单线程（高负载期间 `/health` 可能不响应），启停脚本依据端口状态判定，不会把「忙」误判为「死」 |

---

## 3. 工作原理

学校站点自带 OpenAI 兼容接口，但只接受登录后的凭据（JWT），不接受 API Key。
本程序在本机完成「补凭据 + 归一化协议」：

```
客户端 ──► 127.0.0.1:8787/v1/chat/completions      （本地服务）
                │ 本地 key 校验 · 模型名解析 · 字段归一化 · 附加本人登录凭据
                ▼
      https://chat.hainnu.edu.cn/api/chat/completions（学校 Open WebUI）
                │
                ▼ SSE 收尾 / 用量记账
     标准响应返回客户端
```

登录凭据由 `1.获取令牌.bat` 取得：在本机 Chrome 中由使用者完成学校账号登录，脚本加密写入 `token.txt`。
该步骤不依赖本地服务，本地服务仅读取该文件。

---

## 4. 目录

- [5. 快速开始](#5-快速开始)
- [6. 部署流程](#6-部署流程)
- [7. 客户端配置](#7-客户端配置)
- [8. 在 Agent 框架中配置](#8-在-agent-框架中配置)
- [9. 参数与限制](#9-参数与限制)
- [10. 配置项](#10-配置项)
- [11. 安全与隐私](#11-安全与隐私)
- [12. 故障排查](#12-故障排查)
- [13. 分发与文件清单](#13-分发与文件清单)
- [14. 许可与声明](#14-许可与声明)
- [15. 附录：给 AI Agent](#15-附录给-ai-agent)

> 面向使用者的正文至第 13 节结束。由 AI Agent 自动部署时，请阅读 `AGENT.md`。

---

## 5. 快速开始

| 项目 | 值 |
| --- | --- |
| 服务地址 | `http://127.0.0.1:8787/v1`（OpenAI）/ `http://127.0.0.1:8787`（Anthropic） |
| API Key | `sk-hainnu`（本地校验用，可在 `config.json` 的 `local_api_key` 中修改） |
| 模型名 | 无需指定；请求中的模型名由服务端解析为上游实际存在的 id，真实 id 见 `/v1/models` |
| 部署步骤 | ① 下载 Releases 包 ② 人工登录获取令牌 ③ 启动并自检。含 `runtime\` 的包不用单独装依赖；轻量包/源码由 `2.启动代理.bat` 自动安装 |
| 唯一人工步骤 | 获取令牌时需在弹出的 Chrome 中通过学校 CAS 账号登录（Agent 不得代填密码） |
| 分发包 | [Releases 最新版](https://github.com/sapphirestar46/hainnu-proxy/releases/latest)：`hainnu-proxy.zip`（含 `runtime\`，解压即用）或 `hainnu-proxy-scripts.zip`（仅脚本） |

---

## 6. 部署流程

### 6.1 部署位置

可解压至任意目录与任意路径。所有脚本自动探测本机 Python 与 Chrome，不依赖硬编码路径，
路径含空格或中文亦可。Python 探测顺序：`runtime\` → `.venv\` → `py` → `python` → `python3`。

### 6.2 安装依赖（通常可跳过，且已自动化）

含运行时的分发包（`hainnu-proxy.zip`）内含便携 Python `runtime\`，本步骤可跳过。
轻量包（`hainnu-proxy-scripts.zip`）和仓库源码没有 `runtime\`：直接执行 `2.启动代理.bat` 即可，
缺依赖时会自动安装；也可先执行 `0.安装依赖.bat`（创建 `.venv` 并安装 `fastapi` / `uvicorn` / `httpx`，约 13 MB）。

**多数情况下连这一步都不用管**：`2.启动代理.bat` 与管理台的「启动代理」在拉起服务前会先做依赖自检，
原则是**能复用就不下载**：

1. 先把本机找得到的解释器都探一遍 —— 便携运行时、项目 `.venv`、`py -0p` 列出的全部已安装版本、
   conda/miniforge 及其各 env、`%LOCALAPPDATA%\Programs\Python`、PATH 上的 `python` ——
   谁已经装齐 `fastapi` / `uvicorn` / `httpx` 就直接用谁，**一个包都不下**；
2. 都没有才建 `.venv`，且带 `--system-site-packages`：系统里已有的包直接可见，只补缺的；
3. 真要装时 pip 源按 清华 → 阿里 → 官方 依次回退 —— 只写死单一镜像是不够的：实测本机 pip 访问
   清华源会被 403，而 `curl` 请求同一 URL 却是 200，镜像的封锁是"挑客户端"的，
   写死一个源就会让部分机器怎么装都装不上。

- **验收**：含 `runtime\` 的包可跳过本步；轻量包/源码启动成功后，本机已有可用解释器
  （可能是复用的系统 Python，或新建立的 `.venv\Scripts\python.exe`）。

> ⚠️ **仓库源码没有 `runtime\`**（体积过大，不入库）。不要把「Code → Download ZIP」当成便携安装包。
> 用源码或轻量包时请走 `2.启动代理.bat`：它会先自检依赖，缺了再自动安装。
> 若直接 `python hainnu_proxy.py` 且本机没装 `fastapi` / `uvicorn` / `httpx`，才会出现
> `ModuleNotFoundError`，客户端则报「目标计算机积极拒绝」（端口上没人监听）。
> 仍推荐从 [Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest) 取
> `hainnu-proxy.zip`（含 `runtime\`，解压即用）。

> 获取令牌所需的 `playwright`（约 111 MB）不在运行时内，首次执行 `1.获取令牌.bat` 时按需安装，
> 并使用本机已安装的 Chrome，不会额外下载浏览器。

### 6.3 获取令牌（唯一需要人工的步骤）

执行 `1.获取令牌.bat`，在弹出的浏览器中完成学校登录。脚本轮询页面状态，取得令牌后写入 `token.txt`。

- **验收**：`token.txt` 存在且非空。
- 该令牌**不含过期时间**（payload 无 `exp` 字段），通常一次获取可长期使用；需重新获取的情形：
  学校修改密码或清理会话、服务返回 `401`、更换计算机。
- ⚠️ `token.txt` 等同账号凭据，禁止外传，禁止粘贴至对话、日志或代码仓库。

### 6.4 启动服务

执行 `2.启动代理.bat`，**保持控制台窗口开启**（关闭窗口即终止服务）。

该脚本不会无条件终止端口占用进程，而是先探测：已存在健康实例 → 直接退出；
端口在监听但探活失败（单线程，高负载期间不响应 `/health`）→ 判定为「忙」，不做处理。
确认为残留进程且需强制替换时用 `2.启动代理.bat force`；更新代码后用管理台的「重启代理」。

- **验收**：控制台输出 `[ready] http://127.0.0.1:8787/v1  (local api key: sk-hainnu)`。

| 可选方式 | 入口 | 说明 |
| --- | --- | --- |
| 静默后台运行 | `run_hidden.vbs` | 无控制台窗口 |
| 图形管理台 | `start.bat` / `启动管理台.bat` | 启停、重启、自启开关、取令牌、健康自检、流量曲线与用量表 |
| 开机自启 | `4.开机自启.bat` | 在「启动」文件夹创建静默快捷方式；撤销执行 `5.取消自启.bat` |

### 6.5 自检

执行 `3.自检.bat`：健康探测 → OpenAI 用例（`selftest.py`）→ Anthropic 用例（`test_anthropic.py`）→ DSH 请求形状（`test_dsh.py`）。

- **验收**：输出末尾为 `ALL TESTS PASSED`。

服务可用后，按第 7 节在客户端中填写参数即可；也可使用 `一键配置/` 下的 6 个脚本自动写入（§8.6）。

---

## 7. 客户端配置

> 本节列出客户端侧需填写的参数与服务端的解析规则。各客户端（Claude Code、Cline、OpenCode、DSH、Cursor 等）
> 的配置位置与字段名由该软件决定，本程序不参与、也无法代为完成；此处仅给出通用取值。

### 7.1 OpenAI 兼容接口

| 项目 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | `sk-hainnu` |
| 模型 | 由服务端解析为上游实际存在的 id；真实 id 见 `/v1/models` |

模型名完全跟随上游，不存在静态映射表。服务端每次从学校 `/api/models` 拉取实时列表，
按下列顺序将请求中的模型名解析为上游实际存在的 id：

```
aliases 别名表  >  精确匹配  >  去除 ":tag" 后匹配  >  名称相似度
  >  passthrough_unknown_model（默认 false，即不透传未知名）
  >  default_model（默认留空）  >  上游列表首项
```

若上游返回 `Model not found`，服务端刷新列表、重新解析并重试一轮。

**需要明确的语义**：该机制把请求中的模型名**重定向到上游实际存在的模型**，不提供请求中所写的模型本身。
例如填写 `gpt-4o` 不会失败，但实际执行的是解析后得到的上游模型（无法匹配时为列表首项）。
上游仅单一模型时无歧义；存在多个模型时，若需确定性地指定某一模型，请在 `aliases` 中显式绑定
（如 `{"my-model": "deepseek-flash"}`），或在客户端直接填写 `/v1/models` 返回的真实 id。

### 7.2 Anthropic 协议（Claude Code / Cline / OpenCode 等）

| 协议 | 端点 | 填写方式 |
| --- | --- | --- |
| OpenAI | `/v1/chat/completions` | Base URL `http://127.0.0.1:8787/v1`，Key `sk-hainnu` |
| Anthropic | `/v1/messages` | Base URL `http://127.0.0.1:8787`，请求头 `x-api-key: sk-hainnu` |

以 Claude Code 为例（PowerShell）：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_API_KEY  = "sk-hainnu"
$env:ANTHROPIC_MODEL    = "deepseek-flash"   # 其他名称会被解析到上游实际模型，见 §7.1
claude
```

已验证：文本生成、多轮对话、system 提示 ✅｜工具调用完整回合（`tool_use → tool_result → 组织答案`）✅｜
流式事件序列 ✅｜JSON 模式与 `temperature` / `max_tokens` / `stop_sequences` ✅｜`/v1/messages/count_tokens` ✅（估算值）。

思维链默认不透出；如需输出，将 `config.json` 的 `anthropic_emit_thinking` 设为 `true`。

### 7.3 DeepSeek-Harness (DSH)

Provider ID `hainnu`（本地标识，可自定义）｜Base URL `http://127.0.0.1:8787/v1`｜协议 `openai`｜
Key `sk-hainnu`｜模型 `deepseek-flash`（其他名称亦可，见 §7.1）。

> DSH 以 `role:"developer"` 发送系统提示，并以 `max_completion_tokens` 指定输出上限；
> 服务端自动转换为上游可识别的 `system` 与 `max_tokens`。

**上下文自动压缩**：超限且未处理时上游返回 413（nginx 错误页不会被识别为上下文溢出，DSH 自身的溢出重试无效），
故需启用 DSH 的 `compaction-basic`：

| 文件 | 内容 | 作用 |
| --- | --- | --- |
| `~/.dsh/settings.yaml` | `contextWindow: 260000` | 界面显示的可用上下文；同时作为压缩计算的分母 |
| `~/.dsh/cordis.patch.yml`（全局层） | `thresholdRatio: 0.38` / `retainRatio: 0.12` | 触发点按 260,000 × 0.38 计算 |
| `~/.dsh/profiles/web/cordis.patch.yml` | 同上 | 与全局层保持一致，避免覆盖顺序冲突 |

修改方式：`python set_context_window.py --apply`（默认预演，具备备份、幂等、写入后 YAML 校验与失败回滚）。
⚠️ DSH 会周期性回写 `settings.yaml`，建议先退出 DSH 再执行；完成后重启 DSH 生效。

### 7.4 编程调用

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-hainnu")

r = client.chat.completions.create(
    model="deepseek-chat",                      # 会被解析到上游实际模型，见 §7.1
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in r:
    print(chunk.choices[0].delta.content or "", end="")
```

```bash
curl --noproxy '*' http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-hainnu" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

> `--noproxy '*'`：本机若设置了 `HTTP_PROXY` 等环境变量，访问 `127.0.0.1` 也会被转发至代理，
> 多数本地代理拒绝转发回环地址并返回 403。

---

## 8. 在 Agent 框架中配置

> §8.1–§8.5 以 opencode 为例说明两套链路的并存配置与参数取值；§8.6 提供可选的一键配置脚本
> （覆盖 opencode / WorkBuddy / DeepSeek-Harness）。这些配置项由客户端自身读取：
> **除使用者主动运行 §8.5、§8.6 中的脚本外**，本程序不写入、也不修改任何客户端配置文件。
> 其他支持 OpenAI / Anthropic 协议的框架可按同样方式配置。

### 8.1 链路选择

| 项目 | 经本地服务（日常推荐） | 直连上游（需并行时） |
| --- | --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` | `https://chat.hainnu.edu.cn/api`（**非 `/v1`**） |
| 凭据 | `sk-hainnu`（本地 key，可自定义） | 学校登录 JWT（明文写入客户端配置） |
| 前置条件 | 本地服务运行中 | 不依赖 8787 端口 |
| 优势 | 令牌免维护、限流自动冷却错峰、模型名自愈、协议转换、用量统计 | 不受单线程限制，具备真实并发能力 |
| 限制 | 并发请求被串行化 | 令牌需手动更新；无限流保护、自愈与统计 |
| 速度 | 两条链路基本相当（差异在个位数百分比） | 略快，且首字延迟低约 1 s |

**选型建议**：单会话 Agent（同时仅一个请求在途）用本地服务；需要 N 个并行会话或批量任务时用直连
（直连具备真实并行能力，见 §9.3）。

### 8.2 opencode 双 Provider 配置

配置文件：`~/.config/opencode/opencode.jsonc`（Windows：`C:\Users\<用户名>\.config\opencode\opencode.jsonc`）。
将下列两块加入顶层 `provider`，并将 `<JWT>` 替换为本人学校令牌（获取方式见 §8.5）。

```jsonc
{
  "provider": {
    // ① 经本地服务（日常使用）
    "hainnu": {
      "name": "Hainnu",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-hainnu",
        "timeout": 600000
      },
      "models": {
        "deepseek-flash": {
          "name": "DeepSeek V4.1 Flash (海师)",
          "reasoning": true,
          "tool_call": true,
          "temperature": true,
          "attachment": true,
          "modalities": { "input": ["text", "image"], "output": ["text"] },
          "interleaved": { "field": "reasoning_content" },
          "options": { "reasoningEffort": "low" },      // 默认思考档，须置于该层级
          "limit": { "context": 260000, "output": 32768 },
          "variants": {
            "none": { "reasoningEffort": "none" },
            "low":  { "reasoningEffort": "low" },
            "max":  { "reasoningEffort": "max" }
          }
        }
      }
    },

    // ② 直连学校（需并行吞吐时切换）；模型段字段与 ① 一致，此处为最小可运行配置
    "hainnu-direct": {
      "name": "Hainnu 直连",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://chat.hainnu.edu.cn/api",    // 注意：非 /v1
        "apiKey": "<JWT>",
        "timeout": 600000
      },
      "models": {
        "deepseek-flash": {
          "name": "DeepSeek V4.1 Flash (海师直连)",
          "reasoning": true,
          "tool_call": true,
          "temperature": true,
          "interleaved": { "field": "reasoning_content" },
          "options": { "reasoningEffort": "low" },
          "limit": { "context": 260000, "output": 32768 }
        }
      }
    }
  }
}
```

**配置要点**

- **`baseURL` 写法**：`@ai-sdk/openai-compatible` 自动拼接 `/chat/completions`。本地服务填
  `http://127.0.0.1:8787/v1` → `/v1/chat/completions`；学校填 `https://chat.hainnu.edu.cn/api` →
  `/api/chat/completions`。直连填 `/v1` 将返回 404。
- **模型必须在 `models` 段声明**，opencode 不会主动发现上游模型列表。
- **opencode 不支持 `{env:VAR}` / `{file:...}` 取值**，直连 JWT 只能明文内联，请妥善保管该配置文件。
- `"npm": "@ai-sdk/openai-compatible"` 为协议适配器，不可删除；**改完需重启 opencode** 才加载。
- 直连 `<JWT>` 失效后可用 `更新令牌(opencode直连).bat` 自动写回（opencode 专用，见 §8.5）。

### 8.3 上下文长度取值

| 位置 | 值 | 说明 |
| --- | --- | --- |
| opencode `limit.context` | `260000` | 客户端可见的可用上下文；opencode 会再扣除输出预留，实际输入更小 |
| opencode `limit.output` | `32768` | 单次输出上限 |
| DSH `contextWindow` | `260000` | 压缩计算的分母，须 ≤ 硬上限 |

- 日常 Agent（中英混合 + 代码）：**260000**；纯代码 / 日志负载可放宽至 `320000`；保守或长期无人值守：`200000`；
  **绝对硬上限 `288000`**（贴近时偶发 413）。
- ⚠️ 不可用「4 字符 ≈ 1 token」估算中文输入。
- ⚠️ 客户端若把非 ASCII 转义为 `\uXXXX`（如 Python `json.dumps` 默认行为），中文体积膨胀明显 → 退回 `200000`。

### 8.4 思考档位取值

优先级（由高至低）：**请求体中的客户端取值 > 请求头 `X-Reasoning-Effort` / 查询参数 `?reasoning_effort=` > 服务端 `config.extra_body`**。

- opencode：默认档必须写入**模型级** `"options": { "reasoningEffort": "low" }`；置于模型块顶层字段时将被静默忽略（不报错，也不生效）。
- 临时切换：`opencode run -m hainnu/deepseek-flash --variant max "…"`（`variants` 优先于模型默认档）。
- 档位建议：日常 Agent 用 `low`（响应最快）；需要更强推理时用 `max`；`none` 为最小思考。
  中间档（`minimal` / `medium` / `high` / `xhigh`）表现不稳定，不建议依赖。

### 8.5 直连令牌的获取与更新

直连使用**学校登录 JWT**（非本地 `sk-hainnu`）。获取流程与本地服务无关：
先执行 `1.获取令牌.bat` 完成一次登录（两条链路共用同一 `token.txt`）。

**方式一：脚本更新（opencode / DSH）**

- **opencode**：执行 `更新令牌(opencode直连).bat`。解密 `token.txt` 后，
  在 `~/.config/opencode/opencode.jsonc` 中**定点替换** `hainnu-direct` 块内的 `apiKey`，
  具备时间戳备份、写入后校验与失败自动回滚；完成后重启 opencode 生效。它只改写这一个字段，
  不会创建 provider 或改动其他配置（provider 本身需按 §8.2 自行写入）。
- **DSH**：执行 `更新令牌(DSH直连).bat`。把最新 JWT 写回用户级环境变量
  `HAINNU_DIRECT_API_KEY`（DSH 的直连供应商通过 `apiKeyEnv` 读它，配置文件里不含密钥），
  完成后重开终端 / 重启 DSH 生效。其「经本地服务」的 `hainnu` 供应商对应
  `HAINNU_API_KEY`（本地 `sk-hainnu`，与令牌无关，无需刷新）。

不适用：不使用 opencode / DSH（走方式二）；opencode 走本地服务的 `hainnu` 供应商（本地服务自行读取 `token.txt`，无需执行）；
尚未配置 `hainnu-direct`（opencode 先按 §8.2 完成；DSH 直接运行 §8.6 的「配置到 DeepSeek-Harness(直连学校).bat」即可，
配置时会一并自动写入该环境变量）。

**方式二：手动填写（其他客户端）**

`token.txt` 经 Windows DPAPI 加密，**不可直接复制**，请在**本机**执行（结果仅输出至当前终端）：

```bash
python -c "import token_codec;print(token_codec.decrypt(open('token.txt',encoding='utf-8').read().strip()))"
```

（有便携运行时则用 `runtime\python.exe -c "..."`，轻量包/源码用上面这条或 `.venv\Scripts\python.exe`。）

将输出字符串填入客户端配置的 `apiKey`。

更省事：打开管理台（`启动管理台.bat`），在「连接配置」卡里直接**复制**「直连URL」与
「直连Key」两行——值与本节命令的输出同源（Key 在 GUI 启动时读取一次，重新登录令牌后
需重启管理台刷新）。

⚠️ 该 JWT **不含过期时间**（payload 无 `exp` 字段），通常一次填写长期有效；需重新获取的情形同上 §6.3。
该 JWT 等同账号凭据，禁止外传，禁止粘贴至对话、日志或代码仓库。

### 8.6 一键配置脚本（可选，6 个入口）

`一键配置/` 目录提供 6 个 bat，把接口参数**写入客户端自己的配置文件**，与 §8.2 的手工配置等价，
只是把「找文件 → 定位段落 → 插入 → 备份 → 校验」自动化。

| 入口 | 客户端 | 链路 | 写入内容 |
| --- | --- | --- | --- |
| `配置到 opencode(经本地服务).bat` | opencode | 经本地服务 | `opencode.jsonc` 的 `provider.hainnu` |
| `配置到 opencode(直连学校).bat` | opencode | 直连 | `provider.hainnu-direct`（含明文 JWT） |
| `配置到 WorkBuddy(经本地服务).bat` | WorkBuddy | 经本地服务 | `models.json` 的模型项 |
| `配置到 WorkBuddy(直连学校).bat` | WorkBuddy | 直连 | `models.json` 的模型项（含明文 JWT） |
| `配置到 DeepSeek-Harness(经本地服务).bat` | DSH | 经本地服务 | `settings.yaml` 的 `llm-pi-ai.providers.hainnu` |
| `配置到 DeepSeek-Harness(直连学校).bat` | DSH | 直连 | `llm-pi-ai.providers.hainnu-direct` |

六个入口共用同一核心脚本 `一键配置/_setup_agent.py`，差异仅在 `--agent` 与 `--mode` 两个参数。

**配置文件定位（不写死任何安装位置）**：① 按「用户主目录 + 官方环境变量」推算候选
（`OPENCODE_CONFIG`、`XDG_CONFIG_HOME`、`WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR`、`DSH_CONFIG_DIR`）；
② 多个候选时列出序号供选择；③ 均未找到时提示把配置文件**拖进窗口**或输入路径，也可直接回车新建；
④ 选中的文件若不像目标客户端的配置，会再确认一次。

**写入保证**

- **定点改写**：JSONC 与 YAML 通常带大量注释，脚本只做文本级插入/替换，不整体反序列化再导出（否则丢失全部注释）；
- **幂等 + 可回滚**：重复执行只更新自己那一段，不堆积副本；写前生成 `.bak-<时间戳>` 备份，写后重新读取校验，不通过自动回滚；
- **设为默认模型**：opencode 同时更新顶层 `model`，DSH 写入 `agent-default-model`（WorkBuddy 在界面选模型，无可写默认字段，跳过）；
- 可选参数：`--config "<路径>"` 指定文件、`--dry-run` 只预览不落盘、`--yes` 跳过交互。

**⚠️ 配置完成 ≠ 立即可用**（脚本结束时会逐条列出还缺什么）

| 情况 | 脚本的处理 |
| --- | --- |
| 还没有令牌（`token.txt` 不存在） | 提示获取方式（`1.获取令牌.bat` / 管理台）并询问是否**立即打开取令牌程序**；经本地服务的链路可先写完配置、用之前再补；**直连直接中止**（没有 JWT 无法写入） |
| 经本地服务但服务未运行 | 提示启动方式（`2.启动代理.bat` 并保持窗口开启 / 管理台）并询问是否**立即启动**；配置照常写入 |
| 服务其实在运行 | `/health` 无响应也可能是它正在处理长请求（单线程），脚本会说明，不会误判为「没开」 |
| 直连链路 | 提示 JWT 将**明文**写入客户端配置（等同账号密码，仅限本人），并要求确认 |
| 任何链路 | 提示**重启客户端**（配置只在启动时读取）；DSH 的密钥环境变量由脚本**自动写入**用户级环境变量（重开终端 / 重启 DSH 生效），令牌轮换后用 `更新令牌(DSH直连).bat` 刷新 |

> 前置条件检查只探测本机 `127.0.0.1` 的 `/health`，不向学校服务器发起任何请求。

### 8.7 配置验证

```bash
opencode models hainnu                                  # 列出 deepseek-flash → provider 已识别
opencode run -m hainnu/deepseek-flash "只回答两个字：通了"
opencode run -m hainnu-direct/deepseek-flash --variant max "1+1=?"
```

---

## 9. 参数与限制

> 以下为长期使用中确认的参数与限制。**在其他项目中调用本接口时，请依据本节设计，而非模型标称窗口。**

### 9.1 模型本体

| 参数 | 值 |
| --- | --- |
| 上游模型 ID | `deepseek-flash`（对应官方 **DeepSeek-V4.1-Flash**） |
| 输入上下文窗口 | 1,048,576 tokens（1M） |
| 输出上限 | 65,536 tokens（64k） |
| 架构 | 304B MoE |

> 模型名由上游实时下发：`config.json` 中 `default_model` / `aliases` / `static_models` 留空即跟随上游，
> 上游改名时服务端自动重新解析（解析顺序见 §7.1）。

### 9.2 限制与取值（学校部署）

| 项目 | 值 |
| --- | --- |
| 首个硬限制 | **学校侧请求体上限 1 MiB**。超限后直接返回 `413`，服务端原样透传 —— 表现为请求失败，而非内容截断 |
| 上下文建议取值 | **26 万**（硬上限 **288,000**；纯代码 / 日志负载可放宽至 32 万） |
| 输出 | `max_tokens=32768` 可用；官方上限 64k；Flash 实际单轮输出通常为数十至数百 tokens |
| 思考模式 | 默认开启（走 `config.extra_body` 的 `medium`）；思考 token 计入 `max_tokens`。各档设置见 §8.4 |
| 能力 | **≈ 官方思考档水平**；工具调用、JSON 模式、流式、多轮对话均可用 |

### 9.3 链路性能

- **速度**：两条链路的单任务速度基本相当，差异在个位数百分比；经本地服务的首字延迟略高（约 +1 s）。
- **并发**：经本地服务为单线程，并发请求会被串行排队 —— **需要并行时应使用直连**；直连具备真实并行能力，多并发下未见失败。
- **限流**：常规负载与长时程高负载下均未触发 429；学校侧偶发限流时，服务端自动冷却并指数退避错峰。
- **流式**：SSE 正常（`stream_options.include_usage` 时 usage 完整）；客户端中断时服务端正常收尾上游连接，无请求泄漏。
  ⚠️ 正文必定在 `[DONE]` **之前**发出（历史缺陷：正文位于 `[DONE]` 之后时会被客户端整段丢弃）。
- **缓存**：大量输入 token 命中上游 prompt cache，费用统计按**实测命中率**计算；管理台的「花费 / 累计节省」按官方价折算（每次启动自动获取，见 §2）。
- **图像输入属上游模型能力**：服务端不解析也不改写 `messages`，图像按 OpenAI `image_url` 格式原样转发；支持该格式的客户端可声明 `attachment: true` 与 `modalities.input: ["text","image"]` 直接发送。
- 工具调用完整回合、JSON 模式、多轮对话、Anthropic 协议转换均可用；上下文与输出上限见 §9.2。

### 9.4 在其他项目中调用时的设计边界

1. **输入侧按「请求体 ≤ 900 KB（预留 10% 余量）」设计**，超出则分段；token 数按 **26 万以内**把控（纯代码负载可至 32 万）。
   ⚠️ 不可用「4 字符 ≈ 1 token」估算中文输入。
2. **输出侧** `max_tokens` 取 8k~16k 较稳妥（32k 已验证；输入先受 1 MiB 限制，输出设为 64k 亦不挤占窗口）。
3. **错误语义**：`413` = 请求体超限（缩减输入）；`402` = 学校后端异常（等待恢复）；`401` = 令牌失效（重新获取）。均**原样透传**。
4. 工具调用、JSON 模式、流式、多轮对话、Anthropic 协议均可用。

---

## 10. 配置项

| 字段 | 说明 |
| --- | --- |
| `upstream` | 学校站点地址 |
| `port` | 本地监听端口，默认 `8787` |
| `local_api_key` | 本地校验用 key，留空则不校验 |
| `default_model` | 无法匹配时的回退目标；**留空 = 回退到上游可用模型**（推荐留空）。完整解析顺序见 §7.1 |
| `aliases` | 可选静态别名表，如 `{"my-model":"deepseek-flash"}`；默认为空。优先级高于任何自动解析 |
| `passthrough_unknown_model` | 是否把无法匹配的模型名**原样透传**给上游。默认 `false`（改为回退）；设为 `true` 时上游将直接返回 `Model not found` |
| `extra_body` | 每次请求附加注入的字段，**优先级最低**（客户端已提供时不被覆盖）。默认 `{"reasoning_effort": "medium"}` |
| `timeout` | 上游请求超时秒数，默认 `300` |
| `respect_proxy_env` | 是否读取系统代理环境变量连接学校。**默认 `false`（直连）**；经本地代理中转时上游常返回 `402`，仅内网确实需要代理时设为 `true` |
| `rate_limit_per_minute` / `max_concurrency` | 本地自我保护（固定窗口限流 / 并发上限），`0` = 不限 |
| `local_keys` | 多把本地 key（可选，用于分账统计） |
| `anthropic_emit_thinking` | 是否在 Anthropic 响应中输出思维链，默认 `false`（见 §7.2） |

> 未列出的字段（`token_file`、`host`、`static_models`、`upstream_retries`、`rate_limit_cooldown`、
> `verify_ssl`、`debug_capture`）保持默认值即可。

### 10.1 思考强度（`reasoning_effort`）的分客户端配置

同一服务可为不同客户端下发不同档位，优先级见 §8.4。

| 场景 | 配置方式 | 效果 |
| --- | --- | --- |
| 默认（DSH 等深度任务） | 不额外配置 | 取 `config.json` 的 `extra_body`（当前为 `medium`） |
| 高频往返 Agent（如 OpenCode） | 模型级 `"options": {"reasoningEffort": "low"}` | 每轮无需先等待长思考 |
| 单次临时调整 | `curl -H "X-Reasoning-Effort: low" …` 或 `?reasoning_effort=low` | 仅影响本次请求 |
| 不追加任何字段 | `X-Reasoning-Effort: off` | 连 `config` 兜底值也不注入 |

可取值：`low` / `medium` / `high` / `minimal` / `auto`；
`off` / `none` / `disabled` / `no` / `false` / `0` 表示不附加该字段。
**注意**：本部署下思考无法真正关闭（`none` / `minimal` 仍产生思考 token），`off` 仅表示「不额外注入」。

排查时可通过日志中的 `effort=` 确认某次请求最终使用的档位：

```
openai chat model=… stream=True msgs=2 effort=low
openai stream done 12.3s finished=True aborted=False     # finished=False aborted=True = 客户端中途取消
```

---

## 11. 安全与隐私

### 11.1 凭据保管

| 项目 | 位置 | 保护方式 |
| --- | --- | --- |
| 登录令牌（JWT） | `token.txt` | **Windows DPAPI 加密落盘**（`enc:` 前缀，仅当前 Windows 用户可解）；拷贝到别的机器/用户即失效，需重新获取 |
| 登录姓名 | `user_name.txt` | 仅本机问候语显示用；与 `token.txt` 一并被 `.gitignore` 排除 |
| 分发包 | `hainnu-proxy.zip` | **不含任何凭据**，也不含日志与运行产物 |

`token.txt` 等同账号密码：不外发、不上传，也不整目录拷给别人（拷过去也解不开，只会逼对方重新登录）。

### 11.2 加密挡什么、挡不了什么

令牌加密解决的是**文件被拿走**这一类：别人即便拿到你的 `token.txt`，在他自己的机器上也解不开。

它挡不了**服务运行时的暴露面** —— 服务一旦在跑，任何能连到该端口并持有 API Key 的人，
发出去的请求都签着**你的令牌**，用量记在你名下。这是运行时问题，加密管不到。真正要守住的是这三条：

1. **不要改监听地址** —— 默认 `127.0.0.1`，只有本机程序能连。若被改成对外地址且沿用公开默认 Key，
   服务会**拒绝启动**（默认 Key 写在文档里，对外开放等同于不设防）。
2. **默认 API Key 只适用于只监听回环时** —— `sk-hainnu` 是公开值；本机自用无需改，
   一旦要放开监听就必须换成随机字符串（改动后客户端同步改 Key）。
3. **排查完关闭 `debug_capture`** —— 开启后客户端发来的**完整请求正文会明文落盘**到 `debug_capture.log`。

### 11.3 日志都记了什么

| 文件 | 内容 | 含请求正文 |
| --- | --- | --- |
| `hainnu_proxy.log` | 模型名、是否流式、消息条数、思考档位、耗时、限流事件 | 否 |
| `usage_log.jsonl` | 每次请求的输入/输出 token 数、缓存命中数、使用的 Key | 否 |
| `debug_capture.log` | **原始请求正文**（仅 `debug_capture: true` 时产生） | **是** |

### 11.4 本地自我保护（默认关闭）

`rate_limit_per_minute`（每 Key 每分钟请求数）与 `max_concurrency`（同时在途请求数）默认均为 `0`（不限）。
单人本机使用无需开启；若要在同一台机器上跑很重的批量任务，建议设一个上限，
避免把自己的账号顶到学校侧的限流阈值（见 §9.3）。

注意两条链路的节流能力不同：**经本地服务**是单线程串行，并发请求会排队，天然自带节流；
**直连**没有这道节流，批量任务时更容易顶到上游限流，需自行控制频率（链路选择见 §8.1）。

---

## 12. 故障排查

| 现象 | 处理 |
| --- | --- |
| 提示找不到 python 或 `runtime\` 缺失 | 执行 `0.安装依赖.bat`（需系统 Python 3.11+ 且在 PATH 中） |
| `没有令牌…` / `auth_expired` / `401` | 令牌缺失或失效，执行 `1.获取令牌.bat` |
| `取不到模型列表` | 检查网络连通性（是否在校内、能否访问 `chat.hainnu.edu.cn`） |
| 端口被占用 | 修改 `config.json` 的 `port` |
| 启动脚本提示「已有代理在运行 / 端口被占但探活不过」 | 预期设计：单线程长请求期间不响应 `/health` 但端口仍在监听 → 「健康 → 不操作；监听中但不健康 → 判为忙」（早期版本据此 taskkill，是「代理频繁掉线」的主因） |
| 客户端报 `connection refused` | 确认 `2.启动代理.bat` 的窗口仍开启（或已配置自启且已完成登录） |
| `402 Server Connection Error` / 自检 `upstream_chat.ok:false` | **学校侧问题**（上游模型服务返回 402，浏览器发消息亦同样报错）。联系学校信息中心，修复后本地无需改动；期间可执行 `7.恢复监测.bat` 持续探测 |
| `413 Request Entity Too Large` | 输入超过学校侧 **1 MiB 请求体上限**（可容纳 token 数随文本密度变化），需缩减或分段；取值见 §8.3 |
| 返回 200 但 `content` 为空、`reasoning_content` 非空 | **输出上限被思考占用**（思考 token 计入 `max_tokens`）。服务端**有意不将思维链降级为正文**；提高 `max_tokens`（≥1500）或下调该客户端思考档 |
| 上游返回 `Model not found` | 学校已更换模型 id。服务端自动刷新 `/api/models`、重新解析并重试一轮；建议保持 `default_model` / `aliases` 为空 |
| DSH 报 `completed response with no content` | 已修复两处（上游错误原样抛出；流式正文先于 `[DONE]`）。若仍出现，先执行 `3.自检.bat` 确认后端状态 |
| 连接本地端口返回 403 / 超时 | 本机设置了全局代理环境变量，回环请求被转发。处理：`NO_PROXY=localhost,127.0.0.1,::1`，curl 加 `--noproxy '*'` |
| 一键配置脚本没找到配置文件 | 客户端可能装在非默认位置。运行 `配置到 …bat --config "<完整路径>"`，或在提示处把配置文件拖进窗口；见 §8.6 |
| 一键配置写入后回滚 | 写后重新读取校验未通过（多为目标文件原本就不是合法 JSON/YAML）。已从 `.bak-<时间戳>` 恢复，修复语法后重试 |
| 管理台用量表为空 | 当前时间窗内确实没有请求（例如当天尚未调用）。切换到更长的窗口（1 周 / 1 月）即可看到更早的记录 |

自检端点：`http://127.0.0.1:8787/health`（附加 `?probe=1` 时实际向上游发起一次请求）。

---

## 13. 分发与文件清单

到 **[Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest)** 下载，不要把仓库源码 ZIP 当安装包。

| 文件 | 内容 |
| --- | --- |
| `hainnu-proxy.zip` | 脚本 + 便携 Python `runtime/`，解压即用 |
| `hainnu-proxy-scripts.zip` | 仅脚本。本机已有 Python 时用；启动时会自动准备依赖 |

两包都不含凭据与运行产物。由开发目录中的 `build_portable.py` 生成（该脚本本身不随包分发）。
完整文件清单与分发取舍见 [`AGENT.md`](AGENT.md)「脚本速查 / 分发说明」。

---

## 14. 许可与声明

**许可证**：**MIT**（见 `LICENSE`）—— 代码可自由使用、修改与分发。

需明确区分：MIT 授权的对象是**代码**；**账号凭据与请求中转不在授权范围内**
（见首部「使用约束」及下文「风险承担警告」）。

### 14.1 使用资格与红线

1. **仅限海南师范大学本校师生**：须持有校园账号并能通过学校 CAS 登录。
2. **服务仅监听 `127.0.0.1`，仅服务于本机**：不得修改监听地址或以任何方式对外开放。
3. **三条红线**（详见首部「使用约束」）：不得外传账号凭据；不得为他人中转或多人共用一份凭据；不得商用转售。

> **限制对象为凭据与中转行为，而非程序本身。** 代码按仓库 LICENSE 授权，可自由使用与改造；
> 用于其他单位的 Open WebUI 实例时需自行适配其登录流程（见 §1）。

### 14.2 致谢

- 上游平台基于 [Open WebUI](https://github.com/open-webui/open-webui)。
- 本地限流、并发上限、按 Key 配额与用量统计的设计思路参考了开源 API 网关项目 **sub2api**（LGPL-3.0）。
  本项目**不包含其任何代码**：其为 Go + Redis 实现，本项目为原创的内存版实现
  （固定窗口计数属通用工程做法），因此不受其许可证约束。

### 14.3 凭据与隐私

- `token.txt` 与登录 JWT 等同于账号密码：**不得上传、不得外传、不得粘贴至对话或 issue**。
- 提交至公开仓库前请确认已忽略：`token.txt`、`usage_log.jsonl`、`savings_state.json`、
  `user_name.txt`、`*.log`、`.venv/`、`runtime/`、`chrome-profile/`、`__pycache__/`。

### 14.4 ⚠️ 风险承担警告

**请求以本人校园账号身份发出，所有行为均记录在本人名下。** 分享凭据给他人（含同学）、
开放服务给他人连接或代为中转、多人共用一份凭据 —— 由此产生的流量滥用、违规内容、资源超额，
以及**账号封停、按校规处分或其他处理，全部由本人承担**，且事后无法区分实际操作者。

本程序不提供也不鼓励账号共享或请求中转；相关能力（对外监听、多用户分发）**在本项目中本就不存在**，
请勿自行改造用于此类用途。

### 14.5 免责声明

- 本项目按**「现状」**提供，作者不对账号被封停或限制、学校资源策略变化导致不可用、数据丢失，
  以及因使用或无法使用本程序产生的任何损失负责。
- 本程序所需权限与本人登录学校网页时**完全一致**，不产生额外权限。**请自行确认使用方式符合学校规定。**
- 文中参数均来自特定部署，学校侧可能随时调整后端或限流策略，请以实际环境为准。
- 若学校方面认定本程序的使用方式违规，请**立即停止使用**并删除本地凭据。

---

## 15. 附录：给 AI Agent

> 本节仅供 **AI Agent** 使用，使用者可跳过。完整内容见 **[`AGENT.md`](AGENT.md)** ——
> 行为准则（应拒绝的请求）、可整段复制的部署指令 S1–S5、故障对照 F1–F8、脚本速查与分发说明。
