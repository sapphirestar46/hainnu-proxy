# 故障排查

| 现象 | 处理 |
| --- | --- |
| 提示找不到 python 或 `runtime\` 缺失 | 执行 `0.安装依赖.bat`（需系统 Python 3.11+ 且在 PATH 中） |
| `没有令牌…` / `auth_expired` / `401` | 令牌缺失或失效，执行 `1.获取令牌.bat` |
| `取不到模型列表` | 检查网络连通性（是否在校内、能否访问 `chat.hainnu.edu.cn`） |
| 端口被占用 | 修改 `config.json` 的 `port` |
| 启动脚本提示「已有代理在运行 / 端口被占但探活不过」 | 预期设计：端口仍在监听时按探活区分忙与死，「健康 → 不操作；监听中但不健康 → 判为忙」（早期版本据此 taskkill，是「代理频繁掉线」的主因） |
| 客户端报 `connection refused` | 确认 `2.启动代理.bat` 的窗口仍开启（或已配置自启且已完成登录） |
| `402 Server Connection Error` / 自检 `upstream_chat.ok:false` | **学校侧问题**（上游模型服务返回 402，浏览器发消息亦同样报错）。联系学校信息中心，修复后本地无需改动；期间可执行 `7.恢复监测.bat` 持续探测 |
| 客户端偶发 `incorrect header check`（日志见 `上游连接失败: … incorrect header check`） | **学校网关偶发**把 SSE 响应标成 `Content-Encoding: gzip` 却未实际压缩（实测约 5%，与客户端是否协商压缩无关）。新版已内置兜底：流读取遇「假 gzip」自动按明文透传，请求不再中断。旧包请更新 |
| `413 Request Entity Too Large` | 输入超过学校侧 **1 MiB 请求体上限**（可容纳 token 数随文本密度变化），需缩减或分段；取值见 [客户端配置](clients.md) |
| 返回 200 但 `content` 为空、`reasoning_content` 非空 | **输出上限被思考占用**（思考 token 计入 `max_tokens`）。服务端**有意不将思维链降级为正文**；提高 `max_tokens`（≥1500）或下调该客户端思考档 |
| 上游返回 `Model not found` | 学校已更换模型 id。服务端自动刷新 `/api/models`、重新解析并重试一轮；建议保持 `default_model` / `aliases` 为空 |
| DSH 报 `completed response with no content` | 已修复两处（上游错误原样抛出；流式正文先于 `[DONE]`）。若仍出现，先执行 `3.自检.bat` 确认后端状态 |
| 连接本地端口返回 403 / 超时 | 本机设置了全局代理环境变量，回环请求被转发。处理：`NO_PROXY=localhost,127.0.0.1,::1`，curl 加 `--noproxy '*'` |
| 一键配置脚本没找到配置文件 | 客户端可能装在非默认位置。运行 `配置到 …bat --config "<完整路径>"`，或在提示处把配置文件拖进窗口；见 [一键配置](clients.md) |
| 一键配置写入后回滚 | 写后重新读取校验未通过（多为目标文件原本就不是合法 JSON/YAML）。已从 `.bak-<时间戳>` 恢复，修复语法后重试 |
| 管理台用量表为空 | 当前时间窗内确实没有请求（例如当天尚未调用）。切换到更长的窗口（1 周 / 1 月）即可看到更早的记录 |

自检端点：`http://127.0.0.1:8787/health`（附加 `?probe=1` 时实际向上游发起一次请求）。

---
