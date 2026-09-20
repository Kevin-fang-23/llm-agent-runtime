#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# Windows 保护块（下面这块对 bash 是注释、对 Windows 的 cmd 是可执行代码）。
# Windows 上若误双击本 .sh，cmd 会执行到提示并退出；请改用 scripts\stop.bat。
# ----------------------------------------------------------------------------
: <<'__WINDOWS_GUARD__'
@echo off
chcp 65001 >nul
echo.
echo [提示] 你正在 Windows 上运行 .sh 脚本，这是给 macOS / Linux 用的。
echo        Windows 请改用： scripts\stop.bat   （双击即可）
echo.
pause
exit /b 1
__WINDOWS_GUARD__
# ============================================================================
#  LLM Agent Runtime —— 一键停止脚本（macOS / Linux 版）
#
#  【用法】
#    chmod +x scripts/stop.sh
#    ./scripts/stop.sh
#    # 或：bash scripts/stop.sh
#
#  【可选环境变量】
#    PORT=8100   只停止指定端口上的服务
#
#  【端口确定顺序】
#    1. 环境变量 PORT
#    2. 启动脚本记录的 data/server.port（start.sh 会自动写这个文件）
#    3. 兜底扫描 8000 ~ 8009
#
#  【停止方式】
#    先按端口找监听中的 PID（lsof / ss），都不可用时按进程名
#    pgrep -f "uvicorn app.main:app" 兜底；先发 TERM，1 秒后仍存活再发 KILL。
# ============================================================================

set -u

# ---------- 定位项目根目录（本脚本在 scripts/ 下，根目录是其上级）----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}" || {
  echo "[错误] 无法进入项目目录：${PROJECT_ROOT}"
  exit 1
}

echo
echo "============================================================"
echo "  LLM Agent Runtime 一键停止"
echo "  项目目录：${PROJECT_ROOT}"
echo "============================================================"
echo

# ---------------------- 确定要停止的端口列表 ----------------------
PORT_FILE="${PROJECT_ROOT}/data/server.port"

if [[ -n "${PORT:-}" ]]; then
  PORT_LIST="${PORT}"
  echo "[1/3] 使用环境变量指定的端口：${PORT}"
elif [[ -f "${PORT_FILE}" ]]; then
  PORT_LIST="$(tr -d '[:space:]' < "${PORT_FILE}")"
  echo "[1/3] 读取启动脚本记录的端口：${PORT_LIST}"
else
  PORT_LIST="$(seq 8000 8009 | tr '\n' ' ')"
  echo "[1/3] 未找到端口记录文件，改为扫描 8000 ~ 8009"
fi

# ------------------ 找监听 PID：lsof -> ss -> pgrep ------------------
# 按端口取 PID：优先 lsof（macOS 自带），其次 ss（Linux 自带）
pids_on_port() {
  local p="$1" found=""
  if command -v lsof >/dev/null 2>&1; then
    found="$(lsof -nP -tiTCP:"${p}" -sTCP:LISTEN 2>/dev/null || true)"
  fi
  if [[ -z "${found}" ]] && command -v ss >/dev/null 2>&1; then
    found="$(ss -ltnp 2>/dev/null | grep ":${p} " \
             | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u || true)"
  fi
  printf '%s' "${found}"
}

echo "[2/3] 正在查找并停止服务进程..."

STOPPED=0
for P in ${PORT_LIST}; do
  PIDS="$(pids_on_port "${P}")"
  if [[ -z "${PIDS}" ]]; then
    continue
  fi
  for PID in ${PIDS}; do
    [[ "${PID}" =~ ^[0-9]+$ ]] || continue
    echo "       端口 ${P} 被进程 ${PID} 占用，正在停止..."
    kill "${PID}" 2>/dev/null || true          # 先发 TERM，给它正常收尾的机会
    sleep 1
    if kill -0 "${PID}" 2>/dev/null; then
      kill -9 "${PID}" 2>/dev/null || true     # 仍存活才强杀
      echo "       已强制停止 PID ${PID}"
    else
      echo "       已停止 PID ${PID}"
    fi
    STOPPED=$((STOPPED + 1))
  done
done

# 兜底：端口没查到，但进程可能还在（比如换了端口）
if [[ "${STOPPED}" -eq 0 ]] && command -v pgrep >/dev/null 2>&1; then
  STRAY="$(pgrep -f "uvicorn app.main:app" 2>/dev/null || true)"
  if [[ -n "${STRAY}" ]]; then
    echo "       端口上未找到监听，但发现残留的服务进程，正在停止：${STRAY}"
    for PID in ${STRAY}; do
      kill "${PID}" 2>/dev/null || true
      sleep 1
      kill -0 "${PID}" 2>/dev/null && kill -9 "${PID}" 2>/dev/null || true
      STOPPED=$((STOPPED + 1))
    done
  fi
fi

# ---------------------------- 结果提示 ----------------------------
echo "[3/3] 清理端口记录文件..."
if [[ -f "${PORT_FILE}" ]]; then
  rm -f "${PORT_FILE}"
  echo "       已删除 ${PORT_FILE}"
else
  echo "       没有端口记录文件，跳过"
fi

echo
if [[ "${STOPPED}" -eq 0 ]]; then
  echo "[提示] 没有找到正在运行的服务进程。"
  echo
  echo "       如果服务确实还在运行，请指定端口重试，例如："
  echo "         PORT=8100 ./scripts/stop.sh"
  echo "       或手动查看占用情况："
  echo "         lsof -nP -iTCP -sTCP:LISTEN | grep 8000"
else
  echo "[完成] 已停止 ${STOPPED} 个进程，服务已关闭。"
fi
echo
echo "  重新启动： ./scripts/start.sh"
echo "============================================================"
echo
