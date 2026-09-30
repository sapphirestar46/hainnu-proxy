# 参数与限制

> 以下为长期使用中确认的参数与限制。**在其他项目中调用本接口时，请依据本节设计，而非模型标称窗口。**

## 模型本体

| 参数 | 值 |
| --- | --- |
| 上游模型 ID | `deepseek-flash`（对应官方 **DeepSeek-V4.1-Flash**） |
| 输入上下文窗口 | 1,048,576 tokens（1M） |
| 输出上限 | 65,536 tokens（64k） |
| 架构 | 304B MoE |

> 模型名由上游实时下发：`config.json` 中 `default_model` / `aliases` / `static_models` 留空即跟随上游，
> 上游改名时服务端自动重新解析（解析顺序见 [客户端配置](clients.md)）。

## 限制与取值（学校部署）

| 项目 | 值 |
| --- | --- |
| 首个硬限制 | **学校侧请求体上限 1 MiB**。超限后直接返回 `413`，服务端原样透传 —— 表现为请求失败，而非内容截断 |
| 上下文建议取值 | **26 万**（硬上限 **288,000**；纯代码 / 日志负载可放宽至 32 万） |
| 输出 | `max_tokens=32768` 可用；官方上限 64k；Flash 实际单轮输出通常为数十至数百 tokens |
| 思考模式 | 默认开启（走 `config.extra_body` 的 `medium`）；思考 token 计入 `max_tokens`。各档设置见 [思考强度](reference.md) |
| 能力 | **≈ 官方思考档水平**；工具调用、JSON 模式、流式、多轮对话均可用 |

## 链路性能

- **速度**：单路生成速度接近直连，经本地服务的首字会多一跳（本机转发开销）。
- **并发**：本地服务热路径为异步转发，多路请求可同时在途，不再串成一条；并行上限仍在学校接口，不会比直连更高。
- **限流**：常规负载与长时程高负载下均未触发 429；学校侧偶发限流时，服务端自动冷却并指数退避错峰。
- **流式**：SSE 正常（`stream_options.include_usage` 时 usage 完整）；客户端中断时服务端正常收尾上游连接，无请求泄漏。
  ⚠️ 正文必定在 `[DONE]` **之前**发出（历史缺陷：正文位于 `[DONE]` 之后时会被客户端整段丢弃）。
- **缓存**：大量输入 token 命中上游 prompt cache，费用统计按**实测命中率**计算；管理台的「花费 / 累计节省」按官方价折算（每次启动管理台时自动获取）。
- **图像输入属上游模型能力**：服务端不解析也不改写 `messages`，图像按 OpenAI `image_url` 格式原样转发；支持该格式的客户端可声明 `attachment: true` 与 `modalities.input: ["text","image"]` 直接发送。
- 工具调用完整回合、JSON 模式、多轮对话、Anthropic 协议转换均可用；上下文与输出上限见 [限制与取值](reference.md)。

## 在其他项目中调用时的设计边界

1. **输入侧按「请求体 ≤ 900 KB（预留 10% 余量）」设计**，超出则分段；token 数按 **26 万以内**把控（纯代码负载可至 32 万）。
   ⚠️ 不可用「4 字符 ≈ 1 token」估算中文输入。
2. **输出侧** `max_tokens` 取 8k~16k 较稳妥（32k 已验证；输入先受 1 MiB 限制，输出设为 64k 亦不挤占窗口）。
3. **错误语义**：`413` = 请求体超限（缩减输入）；`402` = 学校后端异常（等待恢复）；`401` = 令牌失效（重新获取）。均**原样透传**。
4. 工具调用、JSON 模式、流式、多轮对话、Anthropic 协议均可用。

---

## 配置项

| 字段 | 说明 |
| --- | --- |
| `upstream` | 学校站点地址 |
| `port` | 本地监听端口，默认 `8787` |
| `local_api_key` | 本地校验用 key，留空则不校验 |
| `default_model` | 无法匹配时的回退目标；**留空 = 回退到上游可用模型**（推荐留空）。解析顺序见 [客户端配置](clients.md) |
| `aliases` | 可选静态别名表，如 `{"my-model":"deepseek-flash"}`；默认为空。优先级高于任何自动解析 |
| `passthrough_unknown_model` | 是否把无法匹配的模型名**原样透传**给上游。默认 `false`（改为回退）；设为 `true` 时上游将直接返回 `Model not found` |
| `extra_body` | 每次请求附加注入的字段，**优先级最低**（客户端已提供时不被覆盖）。默认 `{"reasoning_effort": "medium"}` |
| `timeout` | 上游请求超时秒数，默认 `300` |
| `respect_proxy_env` | 是否读取系统代理环境变量连接学校。**默认 `false`（直连）**；经本地代理中转时上游常返回 `402`，仅内网确实需要代理时设为 `true` |
| `rate_limit_per_minute` / `max_concurrency` | 本地自我保护（固定窗口限流 / 并发上限），`0` = 不限 |
| `local_keys` | 多把本地 key（可选，用于分账统计） |
| `anthropic_emit_thinking` | 是否在 Anthropic 响应中输出思维链，默认 `false`（见 [客户端配置](clients.md)） |

> 未列出的字段（`token_file`、`host`、`static_models`、`upstream_retries`、`rate_limit_cooldown`、
> `verify_ssl`、`debug_capture`）保持默认值即可。

## 思考强度（`reasoning_effort`）的分客户端配置

同一服务可为不同客户端下发不同档位，优先级见 [思考强度](reference.md#思考强度reasoning_effort的分客户端配置)。

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
