@echo off
rem 注意：本文件必须以 ANSI(GBK) 编码保存！若被编辑器转成 UTF-8，
rem 中文会乱码且 cmd 解析可能出错（echo 前缀被吞、整行被当命令执行）。
setlocal EnableDelayedExpansion
rem ============================================================================
rem  LLM Agent Runtime —— 一键启动脚本（Windows 版）
rem
rem  【用法】
rem    方式一：双击本文件
rem    方式二：命令行执行   scripts\start.bat
rem
rem  【可选环境变量】
rem    PORT        指定端口，默认 8000；若被占用会自动顺延到 8001、8002...
rem    PYTHON_EXE  指定 Python 解释器路径（默认自动探测）
rem    NO_OPEN=1   只打印访问地址，不自动打开浏览器
rem
rem  【脚本会依次做这些事】
rem    [1/6] 识别项目类型与包管理器
rem    [2/6] 探测可用的 Python 解释器
rem    [3/6] 检查依赖，缺失则自动安装
rem    [4/6] 选择可用端口（自动跳过被占用的端口）
rem    [5/6] 后台启动 uvicorn 服务
rem    [6/6] 等待服务就绪后自动打开浏览器
rem
rem  停止服务：关闭弹出的「服务」窗口即可，或按 Ctrl+C。
rem ============================================================================

rem ---------- 定位项目根目录（本脚本在 scripts\ 下，根目录是其上级）----------
set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%"
if errorlevel 1 (
  echo [错误] 无法进入项目目录：%~dp0..
  echo        请确认本脚本仍位于项目的 scripts 目录下。
  pause & exit /b 1
)
set "PROJECT_ROOT=%CD%"
echo.
echo ============================================================
echo   LLM Agent Runtime 一键启动
echo   项目目录：%PROJECT_ROOT%
echo ============================================================
echo.

rem =========================== [1/6] 识别项目类型 ===========================
echo [1/6] 正在识别项目类型与包管理器...

set "PROJ_KIND="
set "PKG_MANAGER="

if exist "%PROJECT_ROOT%\requirements.txt" (
  set "PROJ_KIND=python"
  set "PKG_MANAGER=pip"
  echo        识别为 Python 项目，包管理器：pip（requirements.txt）
) else if exist "%PROJECT_ROOT%\pyproject.toml" (
  set "PROJ_KIND=python"
  findstr /i /c:"[tool.poetry]" "%PROJECT_ROOT%\pyproject.toml" >nul 2>&1
  if !errorlevel! EQU 0 (
    set "PKG_MANAGER=poetry"
    echo        识别为 Python 项目，包管理器：poetry（pyproject.toml）
  ) else (
    set "PKG_MANAGER=pip"
    echo        识别为 Python 项目，包管理器：pip（pyproject.toml）
  )
) else if exist "%PROJECT_ROOT%\package.json" (
  set "PROJ_KIND=node"
  echo        识别为 Node.js 项目
) else (
  echo.
  echo [错误] 未能识别项目类型。
  echo        在当前目录既没有找到 requirements.txt / pyproject.toml，
  echo        也没有找到 package.json。
  echo        请确认本脚本放在项目根目录下的 scripts 文件夹里。
  echo.
  pause & popd & exit /b 1
)

if /i "%PROJ_KIND%"=="node" (
  echo.
  echo [错误] 本项目（LLM Agent Runtime）是 Python 服务，不需要 Node 构建。
  echo        检测到 package.json，脚本暂不支持 Node 项目的一键启动。
  echo        如需运行 Python 服务，请在项目根目录放置 requirements.txt。
  echo.
  pause & popd & exit /b 1
)

rem ======================= [2/6] 探测 Python 解释器 =========================
echo [2/6] 正在探测可用的 Python 解释器...

rem 需要的核心依赖，用于判断某个解释器是否已经装好本项目所需环境
set "DEPS_CHECK=import fastapi, langgraph, uvicorn, sqlalchemy, httpx, jsonschema, openai, mcp"

set "PY="
rem 第一轮：优先选「已经装好依赖」的解释器，避免重复安装、启动更快
for %%C in (
  "%PYTHON_EXE%"
  "python"
  "python3"
  "%USERPROFILE%\anaconda3\envs\agent-runtime\python.exe"
  "%USERPROFILE%\miniconda3\envs\agent-runtime\python.exe"
  "%USERPROFILE%\anaconda3\python.exe"
  "%USERPROFILE%\miniconda3\python.exe"
) do (
  if not defined PY (
    set "CAND=%%~C"
    if not "!CAND!"=="" (
      "!CAND!" -c "!DEPS_CHECK!" >nul 2>&1
      if !errorlevel! EQU 0 (
        set "PY=!CAND!"
        echo        已找到装好依赖的解释器：!CAND!
      )
    )
  )
)

rem 第二轮：没有现成环境，就退而求其次找一个能运行的解释器，稍后自动安装依赖
if not defined PY (
  echo        未找到已装好依赖的解释器，改为寻找任意可用的 Python...
  for %%C in (
    "%PYTHON_EXE%"
    "python"
    "python3"
    "%USERPROFILE%\anaconda3\envs\agent-runtime\python.exe"
    "%USERPROFILE%\miniconda3\envs\agent-runtime\python.exe"
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
  ) do (
    if not defined PY (
      set "CAND=%%~C"
      if not "!CAND!"=="" (
        "!CAND!" -c "print(1)" >nul 2>&1
        if !errorlevel! EQU 0 (
          set "PY=!CAND!"
          echo        可用解释器：!CAND!（尚未安装本项目依赖）
        )
      )
    )
  )
)

if not defined PY (
  echo.
  echo [错误] 没有找到可用的 Python 解释器。
  echo        请检查下面任意一项：
  echo          1. 是否已安装 Python 3.11 或以上版本；
  echo          2. 安装时是否勾选了 "Add Python to PATH"；
  echo          3. 若使用 conda，可先执行  conda activate agent-runtime  再启动本脚本，
  echo             或直接设置环境变量 PYTHON_EXE 指向你的 python.exe，例如：
  echo             set PYTHON_EXE=C:\Users\你的用户名\anaconda3\envs\agent-runtime\python.exe
  echo.
  pause & popd & exit /b 1
)

rem ===================== [3/6] 检查依赖，缺失则自动安装 =====================
echo [3/6] 正在检查依赖...

"%PY%" -c "!DEPS_CHECK!" >nul 2>&1
if !errorlevel! EQU 0 (
  echo        依赖已齐全，无需安装。
) else (
  echo        依赖缺失，正在自动安装（pip install -r requirements.txt）...
  echo        这一步需要联网，首次安装可能需要几分钟，请耐心等待。
  echo.
  "%PY%" -m pip install -r "%PROJECT_ROOT%\requirements.txt"
  if !errorlevel! NEQ 0 (
    echo.
    echo [错误] 依赖安装失败。
    echo        常见原因与处理：
    echo          1. 网络不通或被代理拦截 —— 可改用国内镜像源后重试：
    echo             "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo          2. 没有写入权限 —— 请以普通用户身份运行，或改用虚拟环境；
    echo          3. Python 版本过低 —— 本项目需要 Python 3.11 及以上。
    echo.
    pause & popd & exit /b 1
  )
  echo        依赖安装完成。
)

rem 预检：确认应用本身可以被导入（能提前暴露代码/依赖问题，而不是等到浏览器打不开）
echo        正在预检应用能否加载...
"%PY%" -c "import app.main" 2>&1
if !errorlevel! NEQ 0 (
  echo.
  echo [错误] 应用加载失败（上面已打印具体异常）。
  echo        通常是依赖不完整或代码有误。请先手动执行下面命令确认：
  echo          cd /d "%PROJECT_ROOT%"
  echo          "%PY%" -c "import app.main"
  echo.
  pause & popd & exit /b 1
)

rem ==================== [4/6] 选择可用端口（跳过占用）======================
echo [4/6] 正在选择可用端口...

if not defined PORT set "PORT=8000"
set /a PORT_END=%PORT%+9
set "CHOSEN_PORT="

for /l %%P in (%PORT%,1,%PORT_END%) do (
  if not defined CHOSEN_PORT (
    set "PORT_BUSY="
    call :check_port %%P PORT_BUSY
    if "!PORT_BUSY!"=="1" (
      echo        端口 %%P 已被占用，尝试下一个...
    ) else (
      set "CHOSEN_PORT=%%P"
    )
  )
)

if not defined CHOSEN_PORT (
  echo.
  echo [错误] 端口 %PORT% ~ %PORT_END% 全部被占用，没有可用端口。
  echo        处理办法：
  echo          1. 关闭占用这些端口的程序后重试；
  echo          2. 或指定别的端口启动，例如  set PORT=8100  然后重新运行本脚本。
  echo.
  pause & popd & exit /b 1
)
echo        使用端口：%CHOSEN_PORT%

rem 记录端口，供 stop.bat 精确定位要停止的进程
if not exist "data" mkdir "data" >nul 2>&1
> "data\server.port" echo %CHOSEN_PORT%

rem ====================== [5/6] 后台启动 uvicorn 服务 =======================
echo [5/6] 正在启动服务...

rem 刻意不判断 start 的返回码：start 是异步命令，成功启动后不会重置 errorlevel，
rem 残留值会被 if errorlevel 误判成"启动失败"（本脚本此前就踩了这个坑）。
rem 启动是否成功，由下一步的端口就绪轮询来判定 —— 那才是可靠信号。
start "LLM Agent Runtime 服务" /d "%PROJECT_ROOT%" "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %CHOSEN_PORT%
echo        服务进程已在独立窗口启动，正在等待就绪...

rem =================== [6/6] 等待就绪并自动打开浏览器 ======================
echo [6/6] 正在等待服务就绪...

rem 探测工具：优先系统自带的 curl（Win10 1803+ 默认有），否则退回 python
set "HAS_CURL=0"
where curl >nul 2>&1
if !errorlevel! EQU 0 set "HAS_CURL=1"

set "READY=0"
set "WAITED=0"
for /l %%I in (1,1,60) do (
  if !READY!==0 (
    if "!HAS_CURL!"=="1" (
      curl -s -o nul --max-time 2 "http://127.0.0.1:%CHOSEN_PORT%/"
      if !errorlevel! EQU 0 set "READY=1"
    ) else (
      "%PY%" -c "import sys,urllib.request;urllib.request.urlopen(sys.argv[1],timeout=2)" "http://127.0.0.1:%CHOSEN_PORT%/" >nul 2>&1
      if !errorlevel! EQU 0 set "READY=1"
    )
    if !READY!==0 (
      rem 用 ping 做 1 秒延时（比 timeout 命令更通用）
      ping -n 2 127.0.0.1 >nul
      set /a WAITED+=1
      set /a TICK=!WAITED! %% 10
      if !TICK! EQU 0 echo        仍在等待...（已等 !WAITED! 秒）
    )
  )
)

if "!READY!"=="0" (
  echo.
  echo [错误] 服务启动超时（等待 60 秒仍未响应）。
  echo        请先关掉弹出的「LLM Agent Runtime 服务」窗口，再手动执行下面的命令，
  echo        就能看到具体报错：
  echo          cd /d "%PROJECT_ROOT%"
  echo          "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %CHOSEN_PORT%
  echo.
  echo        常见原因：
  echo          1. 端口被其他程序抢占 —— 换一个端口：set PORT=8100 后重试；
  echo          2. 配置文件 .env 有误 —— 参考 .env.example 检查；
  echo          3. 依赖版本不兼容 —— 重新执行 pip install -r requirements.txt。
  echo.
  pause & popd & exit /b 1
)

echo.
echo ============================================================
echo   启动成功！
echo.
echo   访问地址： http://127.0.0.1:%CHOSEN_PORT%
echo   轨迹页面： http://127.0.0.1:%CHOSEN_PORT%/  （左提交任务，右看轨迹）
echo   API 文档： http://127.0.0.1:%CHOSEN_PORT%/docs
echo.
echo   停止服务：双击 scripts\stop.bat，或直接关闭「LLM Agent Runtime 服务」窗口。
echo ============================================================
echo.

if not "%NO_OPEN%"=="1" (
  echo 正在打开浏览器...
  start "" "http://127.0.0.1:%CHOSEN_PORT%/"
)

popd
pause
exit /b 0

rem ---------------------------------------------------------------------------
rem 子程序：判断端口是否被占用
rem   参数 %1 = 端口号
rem   参数 %2 = 接收结果的变量名（会被设为 1=被占用，0=空闲）
rem
rem 为什么不用 errorlevel 传值：cmd 的内置命令成功时不会重置 errorlevel，
rem 残留值会污染后续的 if errorlevel 判断 —— 这正是本脚本此前"启动命令执行失败"
rem 误报的根因。用变量传值是可靠的。
rem ---------------------------------------------------------------------------
:check_port
set "%~2=0"
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%~1 .*LISTENING" 2^>nul') do set "%~2=1"
exit /b 0
