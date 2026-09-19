"""file_ops 工具：工作区限定的文件读写与列举。

所有路径强制 resolve 后必须落在 WORKSPACE_DIR 内（防 ../ 逃逸），
单文件大小限制 2MB。操作集合刻意做小：read / write / list。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 2 * 1024 * 1024


def _safe_resolve(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not (p == root or root in p.parents):
        raise PermissionError(f"路径越界: {rel}（工作区为 {root}）")
    return p


def make_file_ops_handler(workspace: str):
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"]
        path = _safe_resolve(root, args["path"])
        if action == "read":
            if not path.is_file():
                raise FileNotFoundError(f"文件不存在: {args['path']}")
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
            return {"result": text, "summary": f"读取 {args['path']}（{len(text)} 字符）"}

        if action == "write":
            content = args.get("content", "")
            data = content.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("写入内容超过 2MB 限制")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"result": f"已写入 {args['path']}（{len(data)} 字节）", "summary": f"写入 {args['path']}"}

        if action == "list":
            target = path if path.is_dir() else root
            items = []
            for p in sorted(target.iterdir()):
                kind = "dir" if p.is_dir() else "file"
                size = p.stat().st_size if p.is_file() else 0
                items.append({"name": p.name, "kind": kind, "size": size})
            return {"result": items[:100], "summary": f"目录 {target.name} 含 {len(items)} 项"}

        raise ValueError(f"不支持的操作: {action}（可用 read/write/list）")

    return handler


FILE_OPS_SPEC_KWARGS = dict(
    name="file_ops",
    description="在工作区内进行文件操作：read（读文件）、write（写文件）、list（列目录）。",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list"]},
            "path": {"type": "string", "description": "相对工作区的路径，如 reports/out.md"},
            "content": {"type": "string", "description": "write 时的文件内容"},
        },
        "required": ["action", "path"],
    },
    key_result=True,
    key_output_limit=800,
)
