# 部署

## 部署位置

可解压至任意目录与任意路径。所有脚本自动探测本机 Python 与 Chrome，不依赖硬编码路径，
路径含空格或中文亦可。Python 探测顺序：`runtime\` → `.venv\` → `py` → `python` → `python3`。

## 安装依赖（通常可跳过，且已自动化）

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

## 获取令牌（唯一需要人工的步骤）

执行 `1.获取令牌.bat`，在弹出的浏览器中完成学校登录。脚本轮询页面状态，取得令牌后写入 `token.txt`。

- **验收**：`token.txt` 存在且非空。
- 该令牌**不含过期时间**（payload 无 `exp` 字段），通常一次获取可长期使用；需重新获取的情形：
  学校修改密码或清理会话、服务返回 `401`、更换计算机。
- ⚠️ `token.txt` 等同账号凭据，禁止外传，禁止粘贴至对话、日志或代码仓库。

## 启动服务

执行 `2.启动代理.bat`，**保持控制台窗口开启**（关闭窗口即终止服务）。

该脚本不会无条件终止端口占用进程，而是先探测：已存在健康实例 → 直接退出；
端口在监听但探活失败 → 判定为「忙」，不做处理。
确认为残留进程且需强制替换时用 `2.启动代理.bat force`；更新代码后用管理台的「重启代理」。

- **验收**：控制台输出 `[ready] http://127.0.0.1:8787/v1  (local api key: sk-hainnu)`。

| 可选方式 | 入口 | 说明 |
| --- | --- | --- |
| 静默后台运行 | `run_hidden.vbs` | 无控制台窗口 |
| 图形管理台 | `start.bat` / `启动管理台.bat` | 启停、重启、自启开关、取令牌、健康自检、流量曲线与用量表 |
| 开机自启 | `4.开机自启.bat` | 在「启动」文件夹创建静默快捷方式；撤销执行 `5.取消自启.bat` |

## 自检

执行 `3.自检.bat`：健康探测 → OpenAI 用例（`selftest.py`）→ Anthropic 用例（`test_anthropic.py`）→ DSH 请求形状（`test_dsh.py`）。

- **验收**：输出末尾为 `ALL TESTS PASSED`。

服务可用后，客户端填写见 [客户端配置](clients.md)；也可使用 [一键配置脚本](clients.md#一键配置脚本可选6-个入口)。

---

## 分发

到 **[Releases](https://github.com/sapphirestar46/hainnu-proxy/releases/latest)** 下载，不要把仓库源码 ZIP 当安装包。

| 文件 | 内容 |
| --- | --- |
| `hainnu-proxy.zip` | 脚本 + 便携 Python `runtime/`，解压即用 |
| `hainnu-proxy-scripts.zip` | 仅脚本。本机已有 Python 时用；启动时会自动准备依赖 |

两包都不含凭据与运行产物。由开发目录中的 `build_portable.py` 生成（该脚本本身不随包分发）。
完整文件清单与分发取舍见 [`AGENT.md`](AGENT.md)「脚本速查 / 分发说明」。

---
