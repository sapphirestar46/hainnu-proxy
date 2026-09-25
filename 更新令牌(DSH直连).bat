@echo off
cd /d "%~dp0"
title 更新 DSH 直连令牌

rem ============================================================
rem  【本脚本只服务于 DeepSeek-Harness (DSH)】
rem  把学校登录 JWT 写进用户级环境变量 HAINNU_DIRECT_API_KEY
rem  （DSH 的「直连」供应商 hainnu-direct 通过 apiKeyEnv 读它）。
rem  其他客户端不使用本脚本：见 README §8.5「方式二」。
rem
rem  背景：一键配置 DSH(直连) 时会自动写入该变量一次；之后令牌
rem  轮换（重新跑了 1.获取令牌.bat）只需再跑本脚本刷新即可，
rem  不必重新配置 DSH。
rem
rem  它做的事：读项目里的 token.txt（DPAPI 加密）→ 解出 JWT →
rem  写入用户级环境变量并广播系统变更（值不回显）。
rem
rem  只走桥的 hainnu 供应商不需要跑这个 —— 桥自己读 token.txt。
rem ============================================================

call _find_python.bat
if errorlevel 1 (
  echo.
  echo   没找到 Python。请确认 runtime\ 目录完整，或跑一次 0.安装依赖.bat
  echo.
  pause
  exit /b 1
)

echo.
echo   ============================================================
echo     更新 DSH「直连」环境变量  (HAINNU_DIRECT_API_KEY)
echo   ============================================================
echo.

"%PY%" "%~dp0一键配置\_setup_agent.py" --agent dsh --mode direct --refresh-env %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo   [失败] 退出码 %RC%。若提示找不到 token.txt，请先双击「1.获取令牌.bat」。
) else (
  echo   完成。重开终端 / 重启 DSH 后生效。
)
echo.
pause
exit /b %RC%
