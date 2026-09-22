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
rem  【关于授权】
rem    页面**不需要填写任何 API Key**：服务对来自本机（127.0.0.1）的请求免密放行，
rem    双击本脚本后直接就能用。密钥不会出现在本脚本、页面或访问地址里 ——
rem    租户凭据仍在服务端 data\api_credentials.json，仅用于外部来源访问。
rem    若这个浏览器以前填过 API Key 且它已失效（凭据轮换过），页面可能提示"未授权"：
rem    客户端会自动丢弃残留密钥、服务端也会对回环来源的失效密钥回落放行，
rem    启动时的鉴权自检会明确报告这一项是否正常。
rem    注意：用 ngrok / cloudflared 这类**反向隧道**把本机端口暴露到公网时，
rem    本机免密会被一并绕过（隧道由本机主动外连，服务看到的对端仍是回环地址），
rem    外部访客会被当成"本机访问"。要挂公网演示，请给 default 租户设一个小额度，
rem    或在 .env 里设 AUTH_LOCALHOST_BYPASS=false 走严格模式。
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
  echo        依赖缺失，正在自动安装...
  echo        这一步需要联网，首次安装可能需要几分钟，请耐心等待。
  echo.
  rem 优先用锁定文件：声明文件是 >=，装出来的版本随上游漂；
  rem lock 是本地验证过的组合（详见 requirements.lock.txt 头部注释）。
  set "REQ_FILE=%PROJECT_ROOT%\requirements.lock.txt"
  if not exist "!REQ_FILE!" set "REQ_FILE=%PROJECT_ROOT%\requirements.txt"
  echo        使用: !REQ_FILE!
  "%PY%" -m pip install -r "!REQ_FILE!"
  if !errorlevel! NEQ 0 (
    echo.
    echo [错误] 依赖安装失败。
    echo        常见原因与处理：
    echo          1. 网络不通或被代理拦截 —— 可改用国内镜像源后重试：
    echo             "%PY%" -m pip install -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
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

rem ---------- 先回收上一轮遗留的「本项目」服务进程 ----------
rem 为什么必须先回收：下面的端口顺延逻辑遇到占用会跳到 8001。如果 8000 上
rem 还挂着**上一轮启动、但已处于异常状态**的本项目服务，新服务就去 8001 了，
rem 而浏览器里那个旧标签页（或恢复的上次会话）仍然指向 8000 —— 于是页面
rem 连到的是旧进程，表现为「提示未授权 / 提交失败 / 数据对不上」。
rem 这里只回收**确认属于本项目**的进程（命令行含 scripts\serve_dualstack.py
rem 或 app.main），绝不误杀别人的服务。
set "STALE_PIDS="
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING" 2^>nul') do (
  echo !STALE_PIDS! | findstr /r /c:"\<%%A\>" >nul 2>&1
  if errorlevel 1 (
    call :is_our_server %%A
    if "!IS_OURS!"=="1" (
      echo        检测到上一轮遗留的本项目服务（PID %%A，端口 %PORT%），正在回收...
      taskkill /T /F /PID %%A >nul 2>&1
      if !errorlevel! EQU 0 (
        echo        已回收 PID %%A
      ) else (
        echo        回收 PID %%A 失败（可能已被关闭）
      )
    ) else (
      echo        端口 %PORT% 被其他程序占用（PID %%A），将顺延到下一个端口
    )
    set "STALE_PIDS=!STALE_PIDS! %%A"
  )
)
rem 给系统一点时间真正释放端口（TIME_WAIT / 句柄回收）
if defined STALE_PIDS ping -n 2 127.0.0.1 >nul

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

rem ---------- 端口发生了顺延 -> 明确警告，避免浏览器旧标签页连到别人身上 ----------
rem 典型坑：浏览器里存着上次的 http://127.0.0.1:8000 标签（或"恢复上次会话"），
rem 而本轮服务实际在 8001 —— 旧标签页不会自动跳转，看到的可能是另一个程序，
rem 或者干脆是上一轮残留的旧服务，表现为"提示未授权 / 提交失败"。
if not "%CHOSEN_PORT%"=="%PORT%" (
  echo.
  echo   [注意] 端口 %PORT% 被占用，本次服务改用 %CHOSEN_PORT%。
  echo          如果你刚才用的是 http://127.0.0.1:%PORT% 这个地址，
  echo          那个旧标签页指向的不是本次启动的服务 —— 请改用下面的新地址，
  echo          或先双击 scripts\stop.bat 停掉旧服务再用 %PORT% 启动。
)

rem 记录端口，供 stop.bat 精确定位要停止的进程
if not exist "data" mkdir "data" >nul 2>&1
> "data\server.port" echo %CHOSEN_PORT%

rem ====================== [5/6] 后台启动 uvicorn 服务 =======================
echo [5/6] 正在启动服务...

rem 刻意不判断 start 的返回码：start 是异步命令，成功启动后不会重置 errorlevel，
rem 残留值会被 if errorlevel 误判成"启动失败"（本脚本此前就踩了这个坑）。
rem 启动是否成功，由下一步的端口就绪轮询来判定 —— 那才是可靠信号。
rem
rem 【为什么用 serve_dualstack.py 而不是直接 uvicorn --host】
rem 浏览器访问 http://localhost:8000 时，Windows 可能把 localhost 解析成 IPv6 的
rem ::1 而非 127.0.0.1。若服务只监听 127.0.0.1，来自 ::1 的连接会被拒绝 ——
rem 表现就是"页面打不开 / 一直提示未授权"。而 uvicorn 的 --host 只能接受单个地址，
rem 所以用 serve_dualstack.py 同时绑定 127.0.0.1 与 ::1（两个都是回环地址，
rem 仍享有本机免密，不会暴露到局域网）。
start "LLM Agent Runtime 服务" /d "%PROJECT_ROOT%" "%PY%" "scripts\serve_dualstack.py" --port %CHOSEN_PORT%
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
  echo          "%PY%" "scripts\serve_dualstack.py" --port %CHOSEN_PORT%
  echo.
  echo        常见原因：
  echo          1. 端口被其他程序抢占 —— 换一个端口：set PORT=8100 后重试；
  echo          2. 配置文件 .env 有误 —— 参考 .env.example 检查；
  echo          3. 依赖版本不兼容 —— 重新执行 pip install -r requirements.txt。
  echo.
  pause & popd & exit /b 1
)

rem ---------- 鉴权自检：确认「本机免密」真的生效 ----------
rem 这一步是为了把"页面提示未授权"这类问题拦在启动阶段，而不是让用户打开
rem 浏览器后才发现。判定依据是后端 /api/session 的返回值：
rem   mode=passwordless -> 免密生效，页面开箱即用
rem   其他（api_key / 401）-> 配置或来源异常，明确报出来并给出修法
set "AUTH_MODE="
set "STALE_MODE="
set "PROBE_TMP=%TEMP%\agent_runtime_auth_probe.txt"
if not exist "%TEMP%" set "PROBE_TMP=%PROJECT_ROOT%\data\auth_probe.tmp"
rem 取回探测结果用「重定向到临时文件 + set /p 读第一行」，刻意不用 for /f：
rem for /f 的命令首项若是**带引号的 exe 路径**（"%PY%"），cmd 会把那对引号当成
rem 对整个命令的包裹，报"系统找不到指定的路径"（实测踩到，自检静默失效）。
rem 写文件再读没有任何引号解析歧义。
rem
rem 探测一：不带任何密钥（正常本机访问）—— 用 scripts\auth_probe.py 而不是内联
rem python -c：多行/带异常处理的 Python 在 cmd 引号规则下极易写错，中文还可能
rem 撞上 cmd 的 8KB 解析块边界导致解析失步。
"%PY%" "scripts\auth_probe.py" --port %CHOSEN_PORT% --mode > "%PROBE_TMP%" 2>nul
set /p AUTH_MODE=<"%PROBE_TMP%"
rem 探测二：**故意携带一个失效密钥**，模拟浏览器 localStorage 里残留的旧 key。
rem 为什么非测不可：旧实现里密钥优先于本机免密，带失效密钥的请求一律 401，
rem 页面表现为"什么都点不动"；而只测"无密钥"的自检恰好绕开了这条路径、
rem 显示一切正常 —— 这个盲区让问题藏了整整三轮。容错生效时应仍为 passwordless。
"%PY%" "scripts\auth_probe.py" --port %CHOSEN_PORT% --mode --key agent-runtime-stale-key-selfcheck > "%PROBE_TMP%" 2>nul
set /p STALE_MODE=<"%PROBE_TMP%"
del /q "%PROBE_TMP%" >nul 2>&1

echo.
echo ============================================================
echo   启动成功！
echo.
echo   访问地址： http://127.0.0.1:%CHOSEN_PORT%
echo   轨迹页面： http://127.0.0.1:%CHOSEN_PORT%/  （左提交任务，右看轨迹）
echo   API 文档： http://127.0.0.1:%CHOSEN_PORT%/docs
echo.

if "!AUTH_MODE!"=="passwordless" (
  if "!STALE_MODE!"=="passwordless" (
    echo   鉴权状态：本机免密已生效（含"浏览器残留旧密钥"的容错）—— 打开页面即可提交任务。
  ) else (
    echo   [注意] 本机免密已生效，但携带失效密钥的请求被拒（自检：!STALE_MODE!）。
    echo          如果这个浏览器以前填过 API Key，页面会一直报"未授权"、点不动任何按钮。
    echo          修法（任选其一）：
    echo            1. 用最新代码重启服务（app\api\security.py + web\index.html 含回落逻辑）；
    echo            2. 在浏览器里清除本站 localStorage（页面右上角徽标可一键清除）；
    echo            3. 换无痕窗口打开 http://127.0.0.1:%CHOSEN_PORT%/ ，立刻可见差别。
  )
) else if "!AUTH_MODE!"=="disabled" (
  echo   [注意] 鉴权状态：AUTH_ENABLED=false，任何人可提交任务，仅限本机开发使用！
) else if "!AUTH_MODE!"=="api_key" (
  echo   [注意] 鉴权状态：本机免密**未生效**，页面会要求填写 API Key。
  echo          可能原因与修法：
  echo            1. .env 里设了 AUTH_LOCALHOST_BYPASS=false —— 删掉该行或改为 true；
  echo            2. 服务被反向代理接管，请求来源不再是本机回环地址；
  echo            3. 确实需要密钥：从 data\api_credentials.json 取 default_tenant_key，
  echo               点页面右上角徽标填入即可（密钥只存在你的浏览器本地）。
  echo          提示：浏览器里若残留着已失效的旧密钥，页面同样会点不动 ——
  echo               先在右上角清除它，或换无痕窗口验证。
  echo          临时绕过：关掉服务窗口，删除 data\api_credentials.json 后重新双击本脚本，
  echo               会自动重新引导一套新凭据。
) else (
  echo   [注意] 鉴权状态：自检未取到结果（mode=!AUTH_MODE!）。
  echo          多为服务仍在初始化或 /api/session 异常，请稍后刷新页面重试。
)

echo.
echo   停止服务：双击 scripts\stop.bat，或直接关闭「LLM Agent Runtime 服务」窗口。
echo ============================================================
echo.

if not "%NO_OPEN%"=="1" (
  echo 正在打开浏览器...
  rem 带一个随机查询参数：本地演示页前端改得频繁，若浏览器按启发式规则复用了
  rem 缓存里的旧 index.html（旧 JS），就会出现"改了但打开还是老样子"。
  rem 服务端也已对 / 声明 Cache-Control: no-cache，这里再加一道保险。
  start "" "http://127.0.0.1:%CHOSEN_PORT%/?t=%RANDOM%"
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

rem ---------------------------------------------------------------------------
rem 子程序：判断某个 PID 是不是「本项目」的 Agent 服务
rem   参数 %1 = PID
rem   结果写入 IS_OURS（1=是本项目的服务，0=不是/查不到）
rem
rem 为什么必须核对命令行：端口占用者可能是完全无关的程序（别的开发服务、
rem 用户自己的工具）。直接按端口杀进程是危险操作，只回收确认属于本项目的。
rem 判定依据（任一命中即可）：
rem   * 命令行含 serve_dualstack.py  —— 本项目一键启动器的入口
rem   * 命令行含 app.main            —— 直接 uvicorn 起本项目的方式
rem 用 PowerShell 读命令行：wmic 在较新的 Windows 上已被移除（实测本机
rem Win32_Process 可用但 wmic.exe 不存在），PowerShell 是稳定可用的选择。
rem ---------------------------------------------------------------------------
:is_our_server
set "IS_OURS=0"
for /f "usebackq tokens=*" %%C in (`powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=%~1').CommandLine" 2^>nul`) do (
  echo %%C | findstr /i /c:"serve_dualstack.py" >nul 2>&1
  if !errorlevel! EQU 0 set "IS_OURS=1"
  echo %%C | findstr /i /c:"app.main" >nul 2>&1
  if !errorlevel! EQU 0 set "IS_OURS=1"
)
exit /b 0
