@echo off
cd /d "%~dp0"
title 一键配置到 ZCode（直连学校）

rem ============================================================
rem  一键配置：把海师 DeepSeek 写进 ZCode 的模型供应商配置
rem
rem  链路：直连学校
rem   · 用之前需要：已获取登录令牌（1.获取令牌.bat）——直连会把 JWT 写进配置文件
rem  说明：
rem   · 只改 ZCode 的用户级配置（~/.zcode/v2/config.json），不动客户端本身；
rem   · 注意：请先**完全退出 ZCode** 再运行——它退出时可能回写配置，覆盖本次改动；
rem   · 配置文件位置自动查找（也认 ZCODE_CONFIG_DIR），找不到会请你手动指定；
rem   · 写前自动备份、写后校验，不过就回滚；重复执行只是更新，不会堆积副本。
rem   · 可选参数：--config "<配置文件路径>"   --dry-run   --yes
rem ============================================================

call "%~dp0..\_find_python.bat"
if errorlevel 1 (
  echo.
  echo   没找到 Python。请确认 runtime\ 目录完整，或跑一次 ..\0.安装依赖.bat
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0_setup_agent.py" --agent zcode --mode direct %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo   [未完成] 退出码 %RC%。请按上面的提示补齐条件后再运行。
) else (
  echo   完成。重启 ZCode 后，在 设置 → 模型供应商 里选择海师供应商即可。
)
echo.
pause
exit /b %RC%
