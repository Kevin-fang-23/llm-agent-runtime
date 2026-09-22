"""鉴权自检探针：给 start.bat 用的一次性探测工具。

为什么单独成文件而不是塞进 .bat 的 `python -c` 单行：
1. bat 里的多行 Python（尤其带 try/except）在 cmd 的引号规则下几乎不可读、
   极易写错，而且中文注释会在 8KB 解析块边界上引发 cmd 解析失步；
2. 探测逻辑本身值得单独测试（见 tests），放在 .bat 里就没法测。

约定：**输出只有一行纯 ASCII**，便于 bat 的 `for /f` 捕获比较：
    --mode  -> passwordless | api_key | disabled | http_401 | http_000 | error
    默认    -> HTTP 状态码（如 200 / 401），连接失败为 000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def probe(port: int, key: str | None, want_mode: bool, timeout: float = 5.0) -> str:
    url = f"http://127.0.0.1:{port}/api/session"
    req = urllib.request.Request(url)
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body = ""
    except Exception:  # noqa: BLE001 连接被拒 / 超时等：一律报 000，不抛栈
        return "error" if want_mode else "000"

    if not want_mode:
        return str(status)
    if status != 200:
        return f"http_{status}"
    try:
        mode = json.loads(body).get("mode") or "unknown"
    except Exception:  # noqa: BLE001
        return "error"
    return str(mode)


def main() -> int:
    ap = argparse.ArgumentParser(description="鉴权自检探针")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--key", default=None,
                    help="携带该 X-API-Key 探测（用于模拟浏览器里残留的旧密钥）")
    ap.add_argument("--mode", action="store_true", help="输出 mode 字符串而非状态码")
    args = ap.parse_args()
    # 探针绝不能因为异常而污染 bat 的输出流：任何异常都收敛成 ASCII 结果
    sys.stdout.write(probe(args.port, args.key, args.mode) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
