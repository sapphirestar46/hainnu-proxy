# 客户端配置

> 本节列出客户端侧需填写的参数与服务端的解析规则。各客户端（Claude Code、Cline、OpenCode、DSH、Cursor 等）
> 的配置位置与字段名由该软件决定，本程序不参与、也无法代为完成；此处仅给出通用取值。

## OpenAI 兼容接口

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

## Anthropic 协议（Claude Code / Cline 等）

| 协议 | 端点 | 填写方式 |
| --- | --- | --- |
| OpenAI | `/v1/chat/completions` | Base URL `http://127.0.0.1:8787/v1`，Key `sk-hainnu` |
| Anthropic | `/v1/messages` | Base URL `http://127.0.0.1:8787`，请求头 `x-api-key: sk-hainnu` |

以 Claude Code 为例（PowerShell）：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_API_KEY  = "sk-hainnu"
$env:ANTHROPIC_MODEL    = "deepseek-flash"   # 其他名称会被解析到上游实际模型，见 [客户端配置](clients.md)
claude
```

已验证：文本生成、多轮对话、system 提示 ✅｜工具调用完整回合（`tool_use → tool_result → 组织答案`）✅｜
流式事件序列 ✅｜JSON 模式与 `temperature` / `max_tokens` / `stop_sequences` ✅｜`/v1/messages/count_tokens` ✅（估算值）。

思维链默认不透出；如需输出，将 `config.json` 的 `anthropic_emit_thinking` 设为 `true`。

## DeepSeek-Harness (DSH)

Provider ID `hainnu`（本地标识，可自定义）｜Base URL `http://127.0.0.1:8787/v1`｜协议 `openai`｜
Key `sk-hainnu`｜模型 `deepseek-flash`（其他名称亦可，见 [客户端配置](clients.md)）。

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

## 编程调用

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-hainnu")

r = client.chat.completions.create(
    model="deepseek-chat",                      # 会被解析到上游实际模型，见 [客户端配置](clients.md)
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

## 在 Agent 框架中配置

> 以下以 opencode 为例说明两套链路；文末提供可选的一键配置脚本
> （覆盖 opencode / WorkBuddy / DeepSeek-Harness）。这些配置项由客户端自身读取：
> **除使用者主动运行「直连令牌更新」或「一键配置」脚本外**，本程序不写入、也不修改任何客户端配置文件。
> 其他支持 OpenAI / Anthropic 协议的框架可按同样方式配置。

## 链路选择

| 项目 | 经本地服务 | 直连上游 |
| --- | --- | --- |
| Base URL | `http://127.0.0.1:8787/v1` | `https://chat.hainnu.edu.cn/api`（**非 `/v1`**） |
| 凭据 | `sk-hainnu`（本地 key，可自定义） | 学校登录 JWT（明文写入客户端配置） |
| 前置条件 | 本地服务运行中 | 不依赖 8787 端口 |
| 优势 | 本地 key、限流冷却错峰、模型名自愈、Anthropic 协议、用量统计 | 少一跳；OpenAI 兼容客户端可直接填 JWT |
| 限制 | 本机多一跳，首字略慢 | 令牌需手动更新；无限流保护、自愈与统计 |
| 并发 | 异步转发，多路可同时在途；上限由上游决定 | 客户端直接对上游开多条连接；上限同样由上游决定 |

**选型建议**：需要本地 key、Anthropic `/v1/messages`、冷却重试或用量面板 → 走本地服务。
客户端本身就是 OpenAI 兼容、并能把 JWT 当作 apiKey（如 opencode 的 `hainnu-direct`）→ 可直连，不必经 8787。

## opencode 双 Provider 配置

配置文件：`~/.config/opencode/opencode.jsonc`（Windows：`C:\Users\<用户名>\.config\opencode\opencode.jsonc`）。
将下列两块加入顶层 `provider`，并将 `<JWT>` 替换为本人学校令牌（获取方式见 [直连令牌](clients.md)）。

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
- 直连 `<JWT>` 失效后可用 `更新令牌(opencode直连).bat` 自动写回（opencode 专用，见 [直连令牌](clients.md)）。

## 上下文长度取值

| 位置 | 值 | 说明 |
| --- | --- | --- |
| opencode `limit.context` | `260000` | 客户端可见的可用上下文；opencode 会再扣除输出预留，实际输入更小 |
| opencode `limit.output` | `32768` | 单次输出上限 |
| DSH `contextWindow` | `260000` | 压缩计算的分母，须 ≤ 硬上限 |

- 日常 Agent（中英混合 + 代码）：**260000**；纯代码 / 日志负载可放宽至 `320000`；保守或长期无人值守：`200000`；
  **绝对硬上限 `288000`**（贴近时偶发 413）。
- ⚠️ 不可用「4 字符 ≈ 1 token」估算中文输入。
- ⚠️ 客户端若把非 ASCII 转义为 `\uXXXX`（如 Python `json.dumps` 默认行为），中文体积膨胀明显 → 退回 `200000`。

## 思考档位取值

优先级（由高至低）：**请求体中的客户端取值 > 请求头 `X-Reasoning-Effort` / 查询参数 `?reasoning_effort=` > 服务端 `config.extra_body`**。

- opencode：默认档必须写入**模型级** `"options": { "reasoningEffort": "low" }`；置于模型块顶层字段时将被静默忽略（不报错，也不生效）。
- 临时切换：`opencode run -m hainnu/deepseek-flash --variant max "…"`（`variants` 优先于模型默认档）。
- 档位建议：日常 Agent 用 `low`（响应最快）；需要更强推理时用 `max`；`none` 为最小思考。
  中间档（`minimal` / `medium` / `high` / `xhigh`）表现不稳定，不建议依赖。

## 直连令牌的获取与更新

直连使用**学校登录 JWT**（非本地 `sk-hainnu`）。获取流程与本地服务无关：
先执行 `1.获取令牌.bat` 完成一次登录（两条链路共用同一 `token.txt`）。

**方式一：脚本更新（opencode / DSH）**

- **opencode**：执行 `更新令牌(opencode直连).bat`。解密 `token.txt` 后，
  在 `~/.config/opencode/opencode.jsonc` 中**定点替换** `hainnu-direct` 块内的 `apiKey`，
  具备时间戳备份、写入后校验与失败自动回滚；完成后重启 opencode 生效。它只改写这一个字段，
  不会创建 provider 或改动其他配置（provider 本身需按上文「opencode 双 Provider 配置」自行写入）。
- **DSH**：执行 `更新令牌(DSH直连).bat`。把最新 JWT 写回用户级环境变量
  `HAINNU_DIRECT_API_KEY`（DSH 的直连供应商通过 `apiKeyEnv` 读它，配置文件里不含密钥），
  完成后重开终端 / 重启 DSH 生效。其「经本地服务」的 `hainnu` 供应商对应
  `HAINNU_API_KEY`（本地 `sk-hainnu`，与令牌无关，无需刷新）。

不适用：不使用 opencode / DSH（走方式二）；opencode 走本地服务的 `hainnu` 供应商（本地服务自行读取 `token.txt`，无需执行）；
尚未配置 `hainnu-direct`（opencode 先按上文「opencode 双 Provider 配置」完成；DSH 直接运行「配置到 DeepSeek-Harness(直连学校).bat」即可，
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

⚠️ 该 JWT **不含过期时间**（payload 无 `exp` 字段），通常一次填写长期有效；需重新获取的情形见 [部署说明](install.md)。
该 JWT 等同账号凭据，禁止外传，禁止粘贴至对话、日志或代码仓库。

## 一键配置脚本（可选，6 个入口）

`一键配置/` 目录提供 6 个 bat，把接口参数**写入客户端自己的配置文件**，与上文手工配置等价，
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
| 服务其实在运行 | 端口在听但 `/health` 短暂无响应时脚本按「忙」处理，不会误判为「没开」 |
| 直连链路 | 提示 JWT 将**明文**写入客户端配置（等同账号密码，仅限本人），并要求确认 |
| 任何链路 | 提示**重启客户端**（配置只在启动时读取）；DSH 的密钥环境变量由脚本**自动写入**用户级环境变量（重开终端 / 重启 DSH 生效），令牌轮换后用 `更新令牌(DSH直连).bat` 刷新 |

> 前置条件检查只探测本机 `127.0.0.1` 的 `/health`，不向学校服务器发起任何请求。

## 配置验证

```bash
opencode models hainnu                                  # 列出 deepseek-flash → provider 已识别
opencode run -m hainnu/deepseek-flash "只回答两个字：通了"
opencode run -m hainnu-direct/deepseek-flash --variant max "1+1=?"
```

---
