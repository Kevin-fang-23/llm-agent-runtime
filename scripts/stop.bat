@echo off
rem 注意：本文件必须以 ANSI(GBK) 编码保存！若被编辑器转成 UTF-8，
rem 中文会乱码且 cmd 解析可能出错（echo 前缀被吞、整行被当命令执行）。
setlocal EnableDelayedExpansion
rem ============================================================================
rem  LLM Agent Runtime —— 一键停止脚本（Windows 版）
rem
rem  【用法】
rem    方式一：双击本文件
rem    方式二：命令行执行   scripts\stop.bat
rem
rem  【可选环境变量】
rem    PORT=8100   只停止指定端口上的服务（不指定时按下面的顺序查找）
rem
rem  【端口确定顺序】
rem    1. 环境变量 PORT
rem    2. 启动脚本记录的 data\server.port（start.bat 会自动写这个文件）
rem    3. 兜底扫描 8000 ~ 8009（与 start.bat 的端口顺延范围一致）
rem
rem  【停止方式】
rem    端口 -> 监听进程 PID -> taskkill /T /F /PID
rem    这里刻意不用窗口标题匹配：npm/uvicorn 之类会把子窗口标题改成自己的，
rem    按标题杀经常杀不掉真正的进程。
rem ============================================================================

rem ---------- 定位项目根目录（本脚本在 scripts\ 下，根目录是其上级）----------
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%"
if errorlevel 1 (
  echo [错误] 无法进入项目目录：%~dp0..
  pause & exit /b 1
)
set "PROJECT_ROOT=%CD%"

echo.
echo ============================================================
echo   LLM Agent Runtime 一键停止
echo   项目目录：%PROJECT_ROOT%
echo ============================================================
echo.

rem ---------------------- 确定要停止的端口列表 ----------------------
set "PORT_LIST="

if defined PORT (
  set "PORT_LIST=%PORT%"
  echo [1/3] 使用环境变量指定的端口：%PORT%
) else if exist "data\server.port" (
  set /p PORT_LIST=<"data\server.port"
  echo [1/3] 读取启动脚本记录的端口：!PORT_LIST!
) else (
  set "PORT_LIST=8000 8001 8002 8003 8004 8005 8006 8007 8008 8009"
  echo [1/3] 未找到端口记录文件，改为扫描 8000 ~ 8009
)

rem ------------------ 逐个端口：找监听 PID 并停止 ------------------
echo [2/3] 正在查找并停止服务进程...

set "STOPPED=0"
set "KILLED="

for %%P in (%PORT_LIST%) do (
  rem netstat 输出列：协议 / 本地地址 / 外部地址 / 状态 / PID，PID 在第 5 列
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%%P .*LISTENING" 2^>nul') do (
    rem 同一个 PID 可能对应多行，先去重，避免重复提示
    echo !KILLED! | findstr /r /c:"\<%%A\>" >nul 2>&1
    if errorlevel 1 (
      echo        端口 %%P 被进程 %%A 占用，正在停止...
      taskkill /T /F /PID %%A >nul 2>&1
      if !errorlevel! EQU 0 (
        echo        已停止 PID %%A
        set /a STOPPED+=1
      ) else (
        echo        停止 PID %%A 失败（可能已经退出了）
      )
      set "KILLED=!KILLED! %%A"
    )
  )
)

rem ---------------------------- 结果提示 ----------------------------
echo [3/3] 清理端口记录文件...
if exist "data\server.port" (
  del /q "data\server.port" >nul 2>&1
  echo        已删除 data\server.port
) else (
  echo        没有端口记录文件，跳过
)

echo.
if "%STOPPED%"=="0" (
  echo [提示] 没有找到正在运行的服务进程。
  echo.
  echo        如果服务确实还在运行，请确认它占用的端口，例如：
  echo          set PORT=8100
  echo          scripts\stop.bat
  echo        或者手动查看占用情况：
  echo          netstat -ano ^| findstr LISTENING
) else (
  echo [完成] 已停止 %STOPPED% 个进程，服务已关闭。
)
echo.
echo   重新启动：双击 scripts\start.bat
echo ============================================================
echo.

popd
pause
exit /b 0
