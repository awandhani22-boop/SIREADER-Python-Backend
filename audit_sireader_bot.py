from __future__ import annotations

import ast
from pathlib import Path

TARGET = Path('app/approval/telegram_bot.py')
if not TARGET.exists():
    raise SystemExit(f"File tidak ditemukan: {TARGET.resolve()}")

source = TARGET.read_text(encoding='utf-8')
tree = ast.parse(source, filename=str(TARGET))

blocking_names = {
    'list_pending_reviews', 'get_review_detail', 'decide_review',
    'get_runtime_control', 'set_approval_mode', 'pause_auto',
    'set_manual_tp', 'get_manual_tp_setting',
    'ensure_pending_review_notifications', 'claim_next_notification',
    'mark_notification_sent', 'mark_notification_failed',
    'mark_notification_skipped',
}


def calls_in(node):
    found = []
    for child in ast.walk(node):
        if child is node:
            continue
        # Keep nested scopes separately reported, not attributed to the parent.
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else None
            if name in blocking_names:
                found.append((child.lineno, name))
    return sorted(found)

print('=== PEMANGGILAN DB/OUTBOX DI DALAM FUNGSI ASYNC ===')
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef):
        hits = calls_in(node)
        if hits:
            print(f'\n{node.name}() mulai baris {node.lineno}:')
            for line, name in hits:
                print(f'  baris {line}: {name}(...)')

print('\n=== DEFINISI DAN PEMANGGILAN menu_text ===')
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'menu_text':
        print(f'definisi {node.name} pada baris {node.lineno} (async={isinstance(node, ast.AsyncFunctionDef)})')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'menu_text':
        print(f'pemanggilan menu_text() pada baris {node.lineno}')

print('\n=== SEMUA FUNGSI ASYNC DI telegram_bot.py ===')
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef):
        print(f'{node.lineno}: async def {node.name}(...)')

print('\n=== STATUS ===')
print('Audit selesai. Script ini hanya membaca file; tidak mengubah kode maupun database.')
