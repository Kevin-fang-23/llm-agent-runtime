#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Windows 保护块（下面这块对 bash 是注释、对 Windows 的 cmd 是可执行代码）。
# 作用：如果在 Windows 上直接双击本 .sh 文件，cmd 会执行到这里的提示并退出，
#       避免出现 "'xxx' is not recognized as an internal or external command"
#       这类莫名其妙的报错。Windows 用户请改用 scripts\start.bat。
# ----------------------------------------------------------------------------
: <<'__WINDOWS_GUARD__'
@echo off
chcp 65001 >nul
echo.
echo [提示] 你正在 Windows 上运行 .sh 脚本，这是给 macOS / Linux 用的。
echo        Windows 请改用： scripts\start.bat   （双击即可）
echo.
pause
exit /b 1
__WINDOWS_GUARD__

# ============================================================================
#  LLM Agent Runtime —— 一键启动脚本（macOS / Linux 版）
#
#  【用法】
#    方式一：给执行权限后双击或运行
#              chmod +x scripts/start.sh
#              ./scripts/start.sh
#    方式二：直接用 bash 运行（不需要可执行权限）
#              bash scripts/start.sh
#
#  【可选环境变量】
#    PORT        指定端口，默认 8000；若被占用会自动顺延到 8001、8002...
#    PYTHON      指定 Python 解释器（默认自动探测 python3 / python）
#    NO_OPEN=1   只打印访问地址，不自动打开浏览器
#
#  【脚本会依次做这些事】
#    [1/6] 识别项目类型与包管理器
#    [2/6] 探测可用的 Python 解释器
#    [3/6] 检查依赖，缺失则自动安装
#    [4/6] 选择可用端口（自动跳过被占用的端口）
#    [5/6] 后台启动 uvicorn 服务
#    [6/6] 等待服务就绪后自动打开浏览器
#
#  停止服务：在终端按 Ctrl+C，脚本会自动停掉后台服务进程。
# ============================================================================

set -u
# 注意：这里故意不使用 set -e —— 本脚本需要自己接管各类失败并给出中文提示，
# 而不是让 bash 静默退出。

# ---------- 定位项目根目录（本脚本在 scripts/ 下，根目录是其上级）----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}" || {
  echo "[错误] 无法进入项目目录：${PROJECT_ROOT}"
  echo "       请确认本脚本仍位于项目的 scripts 目录下。"
  exit 1
}

echo
echo "============================================================"
echo "  LLM Agent Runtime 一键启动"
echo "  项目目录：${PROJECT_ROOT}"
echo "============================================================"
echo

# =========================== [1/6] 识别项目类型 ===========================
echo "[1/6] 正在识别项目类型与包管理器..."

PROJ_KIND=""
PKG_MANAGER=""

if [[ -f "${PROJECT_ROOT}/requirements.txt" ]]; then
  PROJ_KIND="python"
  PKG_MANAGER="pip"
  echo "       识别为 Python 项目，包管理器：pip（requirements.txt）"
elif [[ -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
  PROJ_KIND="python"
  if grep -qi "\[tool.poetry\]" "${PROJECT_ROOT}/pyproject.toml" 2>/dev/null; then
    PKG_MANAGER="poetry"
    echo "       识别为 Python 项目，包管理器：poetry（pyproject.toml）"
  else
    PKG_MANAGER="pip"
    echo "       识别为 Python 项目，包管理器：pip（pyproject.toml）"
  fi
elif [[ -f "${PROJECT_ROOT}/package.json" ]]; then
  PROJ_KIND="node"
  echo "       识别为 Node.js 项目"
else
  echo
  echo "[错误] 未能识别项目类型。"
  echo "       在当前目录既没有找到 requirements.txt / pyproject.toml，"
  echo "       也没有找到 package.json。"
  echo "       请确认本脚本放在项目根目录下的 scripts 文件夹里。"
  echo
  exit 1
fi

if [[ "${PROJ_KIND}" == "node" ]]; then
  echo
  echo "[错误] 本项目（LLM Agent Runtime）是 Python 服务，不需要 Node 构建。"
  echo "       检测到 package.json，脚本暂不支持 Node 项目的一键启动。"
  echo
  exit 1
fi

# ======================= [2/6] 探测 Python 解释器 =========================
echo "[2/6] 正在探测可用的 Python 解释器..."

# 需要的核心依赖，用于判断某个解释器是否已经装好本项目所需环境
DEPS_CHECK="import fastapi, langgraph, uvicorn, sqlalchemy, httpx, jsonschema, openai, mcp"

PY=""
# 第一轮：优先选「已经装好依赖」的解释器，避免重复安装、启动更快
for CAND in "${PYTHON:-}" python3 python \
           "${HOME}/anaconda3/envs/agent-runtime/bin/python" \
           "${HOME}/miniconda3/envs/agent-runtime/bin/python" \
           "${HOME}/opt/anaconda3/envs/agent-runtime/bin/python"; do
  [[ -z "${CAND}" ]] && continue
  if command -v "${CAND}" >/dev/null 2>&1 || [[ -x "${CAND}" ]]; then
    if "${CAND}" -c "${DEPS_CHECK}" >/dev/null 2>&1; then
      PY="${CAND}"
      echo "       已找到装好依赖的解释器：${CAND}"
      break
    fi
  fi
done

# 第二轮：没有现成环境，就找任意可用解释器，稍后自动安装依赖
if [[ -z "${PY}" ]]; then
  echo "       未找到已装好依赖的解释器，改为寻找任意可用的 Python..."
  for CAND in "${PYTHON:-}" python3 python \
             "${HOME}/anaconda3/envs/agent-runtime/bin/python" \
             "${HOME}/miniconda3/envs/agent-runtime/bin/python" \
             "${HOME}/opt/anaconda3/envs/agent-runtime/bin/python"; do
    [[ -z "${CAND}" ]] && continue
    if command -v "${CAND}" >/dev/null 2>&1 || [[ -x "${CAND}" ]]; then
      PY="${CAND}"
      echo "       可用解释器：${CAND}（尚未安装本项目依赖）"
      break
    fi
  done
fi

if [[ -z "${PY}" ]]; then
  echo
  echo "[错误] 没有找到可用的 Python 解释器。"
  echo "       请检查下面任意一项："
  echo "         1. 是否已安装 Python 3.11 或以上版本（macOS 可用 brew install python@3.11）；"
  echo "         2. 若使用 conda，先执行  conda activate agent-runtime  再运行本脚本；"
  echo "         3. 或直接指定解释器，例如  PYTHON=/opt/homebrew/bin/python3.11 ./scripts/start.sh"
  echo
  exit 1
fi

# ===================== [3/6] 检查依赖，缺失则自动安装 =====================
echo "[3/6] 正在检查依赖..."

if "${PY}" -c "${DEPS_CHECK}" >/dev/null 2>&1; then
  echo "       依赖已齐全，无需安装。"
else
  echo "       依赖缺失，正在自动安装（pip install -r requirements.txt）..."
  echo "       这一步需要联网，首次安装可能需要几分钟，请耐心等待。"
  echo
  if [[ "${PKG_MANAGER}" == "poetry" ]]; then
    poetry install || {
      echo
      echo "[错误] 依赖安装失败（poetry install）。"
      echo "       请检查网络与 pyproject.toml 是否正确。"
      echo
      exit 1
    }
  else
    "${PY}" -m pip install -r "${PROJECT_ROOT}/requirements.txt" || {
      echo
      echo "[错误] 依赖安装失败。"
      echo "       常见原因与处理："
      echo "         1. 网络不通 —— 可改用国内镜像源后重试："
      echo "            ${PY} -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
      echo "         2. 权限不足 —— 建议改用虚拟环境，而不是直接加 sudo；"
      echo "         3. Python 版本过低 —— 本项目需要 Python 3.11 及以上。"
      echo
      exit 1
    }
  fi
  echo "       依赖安装完成。"
fi

# 预检：确认应用本身可以被导入（提前暴露依赖/代码问题）
echo "       正在预检应用能否加载..."
if ! "${PY}" -c "import app.main" 2>&1; then
  echo
  echo "[错误] 应用加载失败（上面已打印具体异常）。"
  echo "       请先手动执行下面命令确认："
  echo "         cd ${PROJECT_ROOT} && ${PY} -c \"import app.main\""
  echo
  exit 1
fi

# ==================== [4/6] 选择可用端口（跳过占用）======================
echo "[4/6] 正在选择可用端口..."

PORT_START="${PORT:-8000}"
PORT_END=$((PORT_START + 9))
CHOSEN_PORT=""

# 端口占用检测：优先 lsof（macOS 常用），否则用 ss（Linux 常用）
port_busy() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${p} "
  else
    # 兜底：尝试建立连接
    (echo > "/dev/tcp/127.0.0.1/${p}") >/dev/null 2>&1
  fi
}

for ((p = PORT_START; p <= PORT_END; p++)); do
  if port_busy "${p}"; then
    echo "       端口 ${p} 已被占用，尝试下一个..."
  else
    CHOSEN_PORT="${p}"
    break
  fi
done

if [[ -z "${CHOSEN_PORT}" ]]; then
  echo
  echo "[错误] 端口 ${PORT_START} ~ ${PORT_END} 全部被占用，没有可用端口。"
  echo "       处理办法："
  echo "         1. 关闭占用这些端口的程序后重试；"
  echo "         2. 或指定别的端口启动，例如  PORT=8100 ./scripts/start.sh"
  echo
  exit 1
fi
echo "       使用端口：${CHOSEN_PORT}"

# 记录端口，供 stop.sh 精确定位要停止的进程
mkdir -p "${PROJECT_ROOT}/data"
echo "${CHOSEN_PORT}" > "${PROJECT_ROOT}/data/server.port"

# ====================== [5/6] 后台启动 uvicorn 服务 =======================
echo "[5/6] 正在启动服务..."

# 日志目录：data/ 已被 .gitignore 忽略，放这里不会污染 git 状态
mkdir -p "${PROJECT_ROOT}/data"
SERVER_LOG="${PROJECT_ROOT}/data/server.log"

nohup "${PY}" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "${CHOSEN_PORT}" \
  >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

# 脚本退出时自动停掉后台服务，避免留下孤儿进程
cleanup() {
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    echo
    echo "正在停止服务（PID ${SERVER_PID}）..."
    kill "${SERVER_PID}" >/dev/null 2>&1
    sleep 1
    kill -0 "${SERVER_PID}" >/dev/null 2>&1 && kill -9 "${SERVER_PID}" >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

# =================== [6/6] 等待就绪并自动打开浏览器 ======================
echo "[6/6] 正在等待服务就绪..."

READY=0
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:${CHOSEN_PORT}/" 2>/dev/null; then
    READY=1
    break
  fi
  # 服务若已崩溃则不必继续等
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ "${READY}" -ne 1 ]]; then
  echo
  echo "[错误] 服务启动超时（等待 60 秒仍未响应）。"
  echo "       服务日志：${SERVER_LOG}"
  echo "       最后 20 行日志如下："
  echo "       ----------------------------------------------------------"
  tail -n 20 "${SERVER_LOG}" 2>/dev/null | sed 's/^/       /'
  echo "       ----------------------------------------------------------"
  echo "       常见原因："
  echo "         1. 端口被抢占 —— 换端口：PORT=8100 ./scripts/start.sh；"
  echo "         2. .env 配置有误 —— 参考 .env.example 检查；"
  echo "         3. 依赖版本不兼容 —— 重新 pip install -r requirements.txt。"
  echo
  exit 1
fi

echo
echo "============================================================"
echo "  启动成功！"
echo
echo "  访问地址： http://127.0.0.1:${CHOSEN_PORT}"
echo "  轨迹页面： http://127.0.0.1:${CHOSEN_PORT}/  （左提交任务，右看轨迹）"
echo "  API 文档： http://127.0.0.1:${CHOSEN_PORT}/docs"
echo "  服务日志： ${SERVER_LOG}"
echo
echo "  停止服务：在本终端按 Ctrl+C（脚本会自动停掉后台进程）"
echo "============================================================"
echo

if [[ "${NO_OPEN:-0}" != "1" ]]; then
  echo "正在打开浏览器..."
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:${CHOSEN_PORT}/" || true          # macOS
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:${CHOSEN_PORT}/" || true      # Linux
  else
    echo "（未找到 open / xdg-open 命令，请手动复制上面的地址到浏览器打开）"
  fi
fi

# 保持前台运行，让 Ctrl+C 能走到 cleanup；同时持续打印心跳，便于观察
echo "服务运行中（PID ${SERVER_PID}）。按 Ctrl+C 停止。"
while kill -0 "${SERVER_PID}" >/dev/null 2>&1; do
  sleep 5
done
echo "服务已退出。"
