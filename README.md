# Hainnu Proxy

把学校已部署的大模型（DeepSeek）接到本机，提供 **OpenAI** 与 **Anthropic** 兼容接口。

[Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest) ·
[部署说明](docs/install.md) · [客户端配置](docs/clients.md) · [参数与限制](docs/reference.md) ·
[故障排查](docs/troubleshooting.md) · [安全策略](SECURITY.md) · [给 AI Agent](AGENT.md)

> **仅限海南师范大学本校师生使用**（须能通过学校 CAS 登录）。
> 代码 MIT 开源；**账号凭据与请求中转不在授权范围内**。
> 禁止外传 `token.txt` / JWT、禁止对外开放端口或为他人中转、禁止多人共用一份凭据。
> 完整条款见下方「许可」。

## 声明

**本库为纯粹 vibe coding 产物。**

**任何问题或想给我加学分请联系：<sapphirestar46@outlook.com>**

## 简介

学校 Open WebUI 只认登录 JWT，不认 API Key。本程序在 `127.0.0.1` 上补凭据、归一化协议、记账，并额外提供 Anthropic `/v1/messages`。权限与本人登录校园网页相同。

```
客户端  →  127.0.0.1:8787  →  chat.hainnu.edu.cn/api/chat/completions
```

- OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages`
- 模型名跟随上游，改名后自动解析
- 限流冷却与重试；用量与官方价折算在管理台
- 异步转发，多路请求可同时在途（上限仍由学校接口决定）

需要本地 Key、Anthropic 或用量面板时走本服务；OpenAI 兼容客户端也可把 JWT 当 apiKey **直连**上游（见 [链路选择](docs/clients.md#链路选择)）。

## 快速开始

从 [Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest) 下载，不要用仓库页的 Code → Download ZIP（没有 `runtime\`）。

| 包 | 说明 |
| --- | --- |
| `hainnu-proxy.zip` | 含便携 Python，解压即用 |
| `hainnu-proxy-scripts.zip` | 仅脚本，本机已有 Python 时用 |

解压后：

1. `1.获取令牌.bat` — 在弹出的 Chrome 里用校园账号登录（唯一需要人工的步骤）
2. `2.启动代理.bat` — **保持窗口开启**；看到 `[ready] http://127.0.0.1:8787/v1`
3. `3.自检.bat` — 末尾应为 `ALL TESTS PASSED`

客户端填写：

| | 值 |
| --- | --- |
| OpenAI Base URL | `http://127.0.0.1:8787/v1` |
| Anthropic Base URL | `http://127.0.0.1:8787` |
| API Key | `sk-hainnu` |
| 模型 | 任意；服务端解析为上游真实 id（见 `/v1/models`） |

图形界面：`启动管理台.bat`。一键写入 opencode / WorkBuddy / DSH：见 [客户端配置](docs/clients.md)。

更细的安装、依赖与开机自启见 [部署说明](docs/install.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/install.md](docs/install.md) | 部署位置、依赖、令牌、启动方式、分发包 |
| [docs/clients.md](docs/clients.md) | OpenAI / Anthropic 参数、模型名解析、Agent 双链路、一键配置 |
| [docs/reference.md](docs/reference.md) | 上下文/输出上限、链路性能、`config.json`、思考档 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 常见故障 |
| [SECURITY.md](SECURITY.md) | 安全报告、凭据与日志 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献范围与不会合并的改动 |
| [AGENT.md](AGENT.md) | 给 AI Agent 的部署指令与红线 |

## 许可

代码 **MIT**（见 [`LICENSE`](LICENSE)）。凭据与中转行为不适用该授权：服务只监听 `127.0.0.1`，请求以本人校园账号发出，违规后果由本人承担。详见首部约束与 [SECURITY.md](SECURITY.md)。

上游基于 [Open WebUI](https://github.com/open-webui/open-webui)。
