"""双栈（IPv4 + IPv6 回环）启动器。

存在理由
--------
`uvicorn --host 127.0.0.1` 只监听 IPv4。浏览器访问 `http://localhost:8000` 时，
Windows 会按 hosts 与 DNS 解析顺序挑地址，`localhost` 既可能给出 `127.0.0.1`
也可能给出 `::1`。若解析结果是 `::1` 而服务只挂了 IPv4，浏览器就会连不上 ——
表现正是"双击启动后页面打不开 / 一直提示未授权"。

`uvicorn` 的 `--host` 只接受单个地址，无法一次挂两个回环地址。所以这里自己
创建两个 socket（AF_INET 与 AF_INET6）分别 bind 到 127.0.0.1 与 ::1 的**同一端口**，
再交给 uvicorn 的 `Server.serve(sockets=[...])`。

实测确认两个 socket 可以 bind 同一端口而不冲突（分别属于不同协议族）。

安全边界（重要）
----------------
只 bind 回环地址，**不 bind 0.0.0.0 / :: 的通配地址** —— 因此不会把服务暴露到
局域网或公网，`AUTH_LOCALHOST_BYPASS` 的"仅本机免密"前提依然成立。
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

# 本脚本位于 scripts/ 下，运行时 sys.path[0] 是 scripts/ 而非项目根，
# 于是 uvicorn 在 import "app.main:app" 时会 ModuleNotFoundError。
# 显式把项目根插到最前，保证无论从哪个 cwd 调用都能正确 import。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _bind(host: str, port: int) -> socket.socket:
    """在指定回环地址上 bind 一个监听 socket。

    显式设 SO_REUSEADDR：进程被强杀后 TIME_WAIT 状态的端口能立刻复用，
    否则"刚关掉服务又立刻双击启动"会撞到端口占用。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # 只接受 IPv6 连接：关掉 dual-stack，避免和 IPv4 socket 抢同一端口
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((host, port))
    s.set_inheritable(True)
    return s


def main() -> int:
    argv = sys.argv[1:]
    host = "127.0.0.1"
    port = 8000
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        else:
            rest.append(argv[i])
            i += 1

    targets = ["127.0.0.1", "::1"]

    sockets: list[socket.socket] = []
    for t in targets:
        try:
            sockets.append(_bind(t, port))
        except OSError as e:
            # 单个地址失败不应致命（例如系统禁用了 IPv6）：
            # 只要还能挂上一个，服务就可用，降级并说明即可。
            print(f"[dualstack] 无法监听 {t}:{port} —— {e}", file=sys.stderr)

    if not sockets:
        print(f"[dualstack] 无法在任何回环地址上监听端口 {port}", file=sys.stderr)
        return 1

    bound = ", ".join(f"{s.getsockname()[0]}:{port}" for s in sockets)
    print(f"[dualstack] 已监听：{bound}", file=sys.stderr)
    if len(sockets) == 1:
        print("[dualstack] 警告：仅挂上单个协议族，"
              "若浏览器把 localhost 解析到另一族会连不上", file=sys.stderr)

    import uvicorn

    # 透传 uvicorn 原生 CLI 参数（--log-level info 等）：--foo bar -> foo="bar"
    extra: dict[str, str] = {}
    for k, v in zip(rest[::2], rest[1::2]):
        extra[k.lstrip("-").replace("-", "_")] = v

    config = uvicorn.Config("app.main:app", host=host, port=port, **extra)
    server = uvicorn.Server(config)
    server.run(sockets=sockets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
