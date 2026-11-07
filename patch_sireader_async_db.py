from __future__ import annotations

import ast
import shutil
import sys
from datetime import datetime
from pathlib import Path


TARGET = Path("app/approval/telegram_bot.py")

# Fungsi sinkron yang dapat melakukan operasi database. Pemanggilan langsung
# dari handler async dipindahkan ke worker thread dengan asyncio.to_thread().
SYNC_DB_FUNCTIONS = {
    "list_pending_reviews",
    "get_review_detail",
    "decide_review",
    "get_runtime_control",
    "set_approval_mode",
    "pause_auto",
    "set_manual_tp",
    "get_manual_tp_setting",
    "ensure_pending_review_notifications",
    "claim_next_notification",
    "mark_notification_sent",
    "mark_notification_failed",
    "mark_notification_skipped",
    # menu_text kemungkinan membaca RuntimeControlState melalui DB.
    "menu_text",
}


def source_offset(source: str, lineno: int, byte_col: int) -> int:
    """Convert AST's UTF-8 byte column to a Python string offset."""
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: lineno - 1])
    line = lines[lineno - 1]
    prefix_bytes = line.encode("utf-8")[:byte_col]
    return start + len(prefix_bytes.decode("utf-8"))


class AsyncCallCollector(ast.NodeVisitor):
    def __init__(self, source: str):
        self.source = source
        self.async_depth = 0
        self.calls: list[ast.Call] = []
        self.parents: dict[ast.AST, ast.AST] = {}

    def visit(self, node):  # build parent map before standard traversal
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        return super().visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.async_depth += 1
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef):
        # A normal function body is synchronous, even when defined inside
        # an async function. Calls there must not receive an `await` prefix.
        previous_depth = self.async_depth
        self.async_depth = 0
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.async_depth = previous_depth

    def visit_Call(self, node: ast.Call):
        if (
            self.async_depth
            and isinstance(node.func, ast.Name)
            and node.func.id in SYNC_DB_FUNCTIONS
        ):
            parent = self.parents.get(node)
            already_threaded = (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and isinstance(parent.func.value, ast.Name)
                and parent.func.value.id == "asyncio"
                and parent.func.attr == "to_thread"
            )
            if not already_threaded:
                self.calls.append(node)

        for child in ast.iter_child_nodes(node):
            self.visit(child)



def main() -> int:
    if not TARGET.is_file():
        print(f"ERROR: File tidak ditemukan: {TARGET.resolve()}")
        print("Jalankan script dari folder C:\\SIREADER.")
        return 1

    original = TARGET.read_text(encoding="utf-8")
    try:
        tree = ast.parse(original, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"ERROR: File awal memiliki syntax error: {exc}")
        return 1

    collector = AsyncCallCollector(original)
    collector.visit(tree)
    calls = collector.calls

    if not calls:
        print("Tidak ada pemanggilan fungsi database sinkron yang perlu diubah.")
        print("File tidak diubah.")
        return 0

    edits = []
    for call in calls:
        func_name = call.func.id
        pieces = [ast.get_source_segment(original, arg) for arg in call.args]
        pieces.extend(ast.get_source_segment(original, kw) for kw in call.keywords)
        pieces = [piece for piece in pieces if piece is not None]
        replacement = f"await asyncio.to_thread({func_name}"
        if pieces:
            replacement += ", " + ", ".join(pieces)
        replacement += ")"

        start = source_offset(original, call.lineno, call.col_offset)
        end = source_offset(original, call.end_lineno, call.end_col_offset)
        edits.append((start, end, replacement, func_name, call.lineno))

    updated = original
    for start, end, replacement, _name, _line in sorted(edits, reverse=True):
        updated = updated[:start] + replacement + updated[end:]

    # Validasi sintaks sebelum menyentuh file asli.
    try:
        ast.parse(updated, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"ERROR: Hasil patch tidak valid; file asli tidak diubah: {exc}")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET.with_name(f"{TARGET.name}.bak_{timestamp}")
    shutil.copy2(TARGET, backup)

    temp = TARGET.with_suffix(TARGET.suffix + ".tmp")
    temp.write_text(updated, encoding="utf-8", newline="")
    temp.replace(TARGET)

    try:
        import py_compile
        py_compile.compile(str(TARGET), doraise=True)
    except Exception as exc:
        shutil.copy2(backup, TARGET)
        print(f"ERROR: py_compile gagal; file asli dipulihkan dari backup: {exc}")
        return 1

    print("PATCH BERHASIL")
    print(f"File diubah : {TARGET.resolve()}")
    print(f"Backup      : {backup.resolve()}")
    print(f"Jumlah call : {len(edits)}")
    print("Pemanggilan yang dipindahkan ke asyncio.to_thread():")
    for _start, _end, _replacement, name, line in sorted(edits, key=lambda x: x[4]):
        print(f"  Baris {line}: {name}(...)")
    print("Syntax      : OK")
    print("\nBerikutnya jalankan: pytest -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
