import logging
import asyncio

from app.signals import trade_decision_watch_worker

from app.approval.outbox import (
    ensure_pending_review_notifications,
    claim_next_notification,
    mark_notification_sent,
    mark_notification_failed,
    mark_notification_skipped,
    list_pending_notification_ids,
)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.config import settings

from app.approval.service import (
    list_pending_reviews,
    get_review_detail,
    decide_review,
    get_runtime_control,
    set_approval_mode,
    pause_auto,
    set_manual_tp,
    get_manual_tp_setting,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("SIREADER_BOT")
# Jangan tampilkan URL Telegram API karena URL tersebut
# dapat berisi token bot.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

import time
import functools

from app.profiling import (
    measure,
    setup_file_logging,
    install_sqlalchemy_events,
    make_profiling_request_class,
    httpx_event_hooks,
)

perf_logger = logging.getLogger("SIREADER_PERF")


def timed_handler(func):
    """
    Mengukur durasi handler Telegram dalam milidetik.

    Log berisi nama handler, durasi, dan status sukses/gagal.
    TIDAK mencatat token, API key, DATABASE_URL, isi pesan, atau
    callback data (menghindari kebocoran data sensitif).
    """

    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        start = time.perf_counter()
        status = "ok"
        try:
            return await func(update, context, *args, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            perf_logger.info(
                "handler=%s status=%s durasi_ms=%.1f",
                func.__name__,
                status,
                elapsed_ms,
            )

    return wrapper


# ============================================================
# AUTHORIZATION
# ============================================================

def is_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if user is None or chat is None:
        return False

    if settings.TELEGRAM_APPROVER_USER_ID is None:
        return False

    # Bot hanya menerima kontrol melalui private chat.
    if chat.type != "private":
        return False

    return user.id == settings.TELEGRAM_APPROVER_USER_ID


async def deny_access(update: Update):
    if update.callback_query:
        await update.callback_query.answer(
            "Akses ditolak. Kamu bukan approver terdaftar.",
            show_alert=True,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Akses ditolak."
        )


def reviewer_name(update: Update) -> str:
    user = update.effective_user

    if user and user.full_name:
        return user.full_name

    return str(user.id if user else "unknown")


# ============================================================
# KEYBOARDS
# ============================================================

def menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 SIGNAL REVIEW",
                callback_data="reviews",
            )
        ],
        [
            InlineKeyboardButton(
                "🛡 APPROVAL MODE",
                callback_data="mode:approval",
            ),
            InlineKeyboardButton(
                "🔒 AUTO MODE",
                callback_data="mode:auto",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏸ PAUSE AUTO",
                callback_data="pause",
            ),
            InlineKeyboardButton(
                "📊 STATUS",
                callback_data="status",
            ),
        ],
    ])


def approval_confirmation_keyboard(analysis_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "? KONFIRMASI APPROVAL",
                callback_data=f"confirmapprove:{analysis_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "?? BATAL",
                callback_data=f"review:{analysis_id}",
            )
        ],
    ])


def review_detail_keyboard(analysis_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ APPROVE (REVIEW)",
                callback_data=f"approve:{analysis_id}",
            ),
            InlineKeyboardButton(
                "❌ REJECT",
                callback_data=f"reject:{analysis_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎯 SET / UPDATE TP",
                callback_data=f"settp:{analysis_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ KEMBALI KE ANTREAN",
                callback_data="reviews",
            )
        ],
    ])


def tp_keyboard(analysis_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "30 PIPS",
                callback_data=f"tp:{analysis_id}:30",
            ),
            InlineKeyboardButton(
                "50 PIPS",
                callback_data=f"tp:{analysis_id}:50",
            ),
            InlineKeyboardButton(
                "100 PIPS",
                callback_data=f"tp:{analysis_id}:100",
            ),
        ],
        [
            InlineKeyboardButton(
                "✏️ INPUT MANUAL",
                callback_data=f"tpcustom:{analysis_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ KEMBALI",
                callback_data=f"review:{analysis_id}",
            )
        ],
    ])


# ============================================================
# TEXT FORMATTERS
# ============================================================

def menu_text():
    state = get_runtime_control()

    return (
        "SIREADER CONTROL PANEL\n"
        "============================\n\n"
        f"Execution mode: {state['execution_mode']}\n"
        f"Trading mode: {state['trading_mode']}\n"
        f"Auto execution: {state['auto_execution_enabled']}\n"
        f"Auto paused: {state['auto_paused']}\n\n"
        "Pilih menu di bawah."
    )


def review_list_text(pending):
    if not pending:
        return (
            "SIREADER SIGNAL REVIEW\n\n"
            "Tidak ada analisis yang menunggu review."
        )

    lines = [
        "SIREADER SIGNAL REVIEW",
        "============================",
        "",
        f"Jumlah antrean: {len(pending)}",
        "",
    ]

    for item in pending:
        confidence = item.get("confidence")

        if isinstance(confidence, (int, float)):
            confidence_text = f"{confidence * 100:.1f}%"
        else:
            confidence_text = "-"

        lines.extend([
            f"ID: #{item['analysis_log_id']}",
            f"Telegram: {item['target_message_id']}",
            f"Direction: {item.get('direction') or '-'}",
            f"Confidence: {confidence_text}",
            f"Ringkasan: {item.get('summary') or '-'}",
            "----------------------------",
        ])

    return "\n".join(lines)


def review_list_keyboard(pending):
    rows = []

    for item in pending:
        analysis_id = item["analysis_log_id"]

        rows.append([
            InlineKeyboardButton(
                f"BUKA REVIEW #{analysis_id}",
                callback_data=f"review:{analysis_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🏠 MENU UTAMA",
            callback_data="menu",
        )
    ])

    return InlineKeyboardMarkup(rows)


def review_detail_text(detail, tp_setting):
    final = detail["final_analysis"]
    extracted = final.get("extracted") or {}

    confidence = final.get("confidence")

    if isinstance(confidence, (int, float)):
        confidence_text = f"{confidence * 100:.1f}%"
    else:
        confidence_text = "-"

    # Decision sudah tersimpan di result_json AIAnalysisLog.
    result_payload = detail.get("result")

    decision = (
        result_payload.get("decision")
        if isinstance(result_payload, dict)
        else None
    )

    if not isinstance(decision, dict):
        decision = {}

    market_data = decision.get("market_data")
    if not isinstance(market_data, dict):
        market_data = {}

    runtime = decision.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}

    lines = [
        "SIREADER SIGNAL REVIEW",
        "============================",
        f"Analysis ID: #{detail['analysis_log_id']}",
        f"Telegram ID: {detail['target_message_id']}",
        f"Status: {detail['status']}",
        "",
        "HASIL ANALISIS",
        "----------------------------",
        f"Direction: {extracted.get('direction') or '-'}",
        f"Entry: {extracted.get('entry_zone_text') or '-'}",
        f"Explicit entry: {extracted.get('explicit_entry_text') or '-'}",
        f"SL mentor: {extracted.get('stop_loss_text') or '-'}",
        f"TP mentor: {extracted.get('take_profit_texts') or 'Belum diberikan'}",
        f"Confidence: {confidence_text}",
        "",
        "DECISION ENGINE",
        "----------------------------",
        f"Decision status: {decision.get('status') or '-'}",
        f"Decision reason: {decision.get('reason') or '-'}",
        f"Trade episode: {decision.get('trade_episode_id') or '-'}",
        f"Market data: {'AVAILABLE' if market_data.get('available') else 'UNAVAILABLE'}",
        f"MT5 symbol: {market_data.get('symbol') or '-'}",
        f"Price side: {market_data.get('price_side') or '-'}",
        f"Reference price: {market_data.get('current_price') or '-'}",
        f"Bid / Ask: {market_data.get('bid') or '-'} / {market_data.get('ask') or '-'}",
        f"Tick age: {market_data.get('tick_age_seconds') if market_data.get('tick_age_seconds') is not None else '-'} seconds",
        f"Market data reason: {market_data.get('reason') or '-'}",
        f"Account mode: {market_data.get('account_mode') or '-'}",
        f"MT5 readonly: {market_data.get('readonly', True)}",
        f"Execution mode: {runtime.get('execution_mode') or '-'}",
        f"Trading mode: {runtime.get('trading_mode') or '-'}",
        f"Auto execution enabled: {runtime.get('auto_execution_enabled', False)}",
        f"Auto paused: {runtime.get('auto_paused', True)}",
        f"Order sent: {decision.get('order_sent', False)}",
        "",
        "MANUAL TP",
        "----------------------------",
    ]

    if tp_setting:
        lines.extend([
            f"TP: {tp_setting['tp_pips']} pips",
            f"Jarak harga: {tp_setting['price_distance']}",
            f"Status: {tp_setting['status']}",
        ])
    else:
        lines.append("Belum diatur.")

    lines.extend([
        "",
        "EVIDENCE",
        "----------------------------",
    ])

    for evidence in final.get("evidence", []):
        lines.append(
            f"[{evidence.get('field')}] "
            f"Pesan {evidence.get('message_id')}: "
            f"{evidence.get('quote')!r}"
        )

    lines.extend([
        "",
        "KETIDAKPASTIAN",
        "----------------------------",
    ])

    uncertainties = final.get("uncertainties") or []

    if uncertainties:
        for item in uncertainties:
            lines.append(f"- {item}")
    else:
        lines.append("- Tidak ada.")

    lines.extend([
        "",
        "PESAN SUMBER",
        "----------------------------",
    ])

    bundle = detail.get("source_bundle") or {}

    for message in bundle.get("messages", []):
        lines.append(
            f"[{message['telegram_message_id']}] "
            f"{message.get('text', '')}"
        )

    lines.extend([
        "",
        "Catatan: APPROVE hanya menyimpan keputusan review.",
        "Belum ada order MT5 yang dikirim.",
    ])

    return "\n".join(lines)[:3900]


# ============================================================
# COMMANDS
# ============================================================

@timed_handler
async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await deny_access(update)
        return

    with measure("start_command.menu_text.to_thread"):
        text = await asyncio.to_thread(menu_text)
    with measure("start_command.reply_text"):
        await update.effective_message.reply_text(
            text,
            reply_markup=menu_keyboard(),
        )


@timed_handler
async def menu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await deny_access(update)
        return

    await update.effective_message.reply_text(
        await asyncio.to_thread(menu_text),
        reply_markup=menu_keyboard(),
    )


@timed_handler
async def reviews_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await deny_access(update)
        return

    with measure("reviews_command.list_pending_reviews.to_thread"):
        pending = await asyncio.to_thread(list_pending_reviews)

    with measure("reviews_command.reply_text"):
        await update.effective_message.reply_text(
            review_list_text(pending),
            reply_markup=review_list_keyboard(pending),
        )


def collect_system_status(notification_worker_running):
    """
    Mengumpulkan diagnostik read-only.
    Tidak mengirim request AI berbayar dan tidak menghubungkan MT5.
    """
    snapshot = {
        "telegram": "ONLINE (command received)",
        "notification_worker": (
            "RUNNING"
            if notification_worker_running
            else "NOT RUNNING"
        ),
        "database": "UNKNOWN",
        "database_error": None,
        "ai_pipeline": "UNKNOWN",
        "ai_pipeline_error": None,
        "outbox_pending": None,
        "outbox_error": None,
        "runtime": None,
        "runtime_error": None,
        "mt5_configured": False,
        "mt5_connection": "NOT CHECKED",
        "mt5_account": "UNKNOWN",
        "approver_configured": False,
    }

    # Runtime control.
    try:
        state = get_runtime_control()
        if isinstance(state, dict):
            snapshot["runtime"] = state
        else:
            snapshot["runtime_error"] = "INVALID_RUNTIME_RESPONSE"
    except Exception as exc:
        snapshot["runtime_error"] = type(exc).__name__

    # Database connectivity.
    try:
        from sqlalchemy import text
        from app.database.connection import SessionLocal

        with SessionLocal() as db:
            db.execute(text("SELECT 1"))

        snapshot["database"] = "OK"
    except Exception as exc:
        snapshot["database"] = "ERROR"
        snapshot["database_error"] = type(exc).__name__

    # Outbox: hanya membaca daftar PENDING.
    try:
        pending = list_pending_notification_ids()
        snapshot["outbox_pending"] = len(pending)
    except Exception as exc:
        snapshot["outbox_error"] = type(exc).__name__

    # Memastikan modul pipeline dapat di-import.
    # Ini BUKAN pengujian provider AI atau request model.
    try:
        import app.ai.pipeline  # noqa: F401
        snapshot["ai_pipeline"] = "MODULE LOADABLE"
    except Exception as exc:
        snapshot["ai_pipeline"] = "IMPORT ERROR"
        snapshot["ai_pipeline_error"] = type(exc).__name__

    # Konfigurasi MT5 saja. Tidak melakukan initialize/connect.
    terminal_path = getattr(settings, "MT5_TERMINAL_PATH", None)
    symbol = getattr(settings, "MT5_SYMBOL", None)

    snapshot["mt5_configured"] = bool(
        terminal_path and symbol
    )

    recipient = getattr(
        settings,
        "TELEGRAM_APPROVER_USER_ID",
        None,
    )
    snapshot["approver_configured"] = bool(
        isinstance(recipient, int)
        and not isinstance(recipient, bool)
        and recipient > 0
    )

    return snapshot


def format_system_status(snapshot):
    """Menyusun laporan diagnostik Telegram dari snapshot."""
    runtime = snapshot.get("runtime")

    if not isinstance(runtime, dict):
        runtime = {}

    def display(value):
        if value is None:
            return "UNKNOWN"
        return str(value)

    database = snapshot.get("database", "UNKNOWN")
    ai_pipeline = snapshot.get("ai_pipeline", "UNKNOWN")
    worker = snapshot.get("notification_worker", "UNKNOWN")
    pending = snapshot.get("outbox_pending")

    lines = [
        "SIREADER SYSTEM STATUS",
        "============================",
        "",
        "RUNTIME",
        "----------------------------",
        f"Execution mode: {display(runtime.get('execution_mode'))}",
        f"Trading mode: {display(runtime.get('trading_mode'))}",
        f"Auto execution: {display(runtime.get('auto_execution_enabled'))}",
        f"Auto paused: {display(runtime.get('auto_paused'))}",
        "",
        "SYSTEM HEALTH",
        "----------------------------",
        f"Telegram Bot: {snapshot.get('telegram', 'UNKNOWN')}",
        f"Database: {database}",
        f"AI Pipeline module: {ai_pipeline}",
        f"Notification Worker: {worker}",
        f"Outbox Pending: {display(pending)}",
        f"Approver configured: {snapshot.get('approver_configured', False)}",
        "",
        "MT5 CONNECTION",
        "----------------------------",
        f"Terminal configuration: {'CONFIGURED' if snapshot.get('mt5_configured') else 'INCOMPLETE'}",
        f"Connection: {snapshot.get('mt5_connection', 'NOT CHECKED')}",
        f"Account mode: {snapshot.get('mt5_account', 'UNKNOWN')}",
        "Connection probe: NOT RUN",
        "Connector policy: READ ONLY",
        "",
        "SAFETY",
        "----------------------------",
        f"Order execution: DISABLED",
        "Order sent by status check: False",
        "",
        "Catatan:",
        "- AI Pipeline module bukan tes provider AI.",
        "- MT5 tidak dihubungkan oleh perintah /status.",
        "- Database dan Outbox diperiksa secara read-only.",
    ]

    # Tampilkan tipe error, bukan detail koneksi atau credential.
    errors = []
    for label, key in (
        ("Database", "database_error"),
        ("AI Pipeline", "ai_pipeline_error"),
        ("Outbox", "outbox_error"),
        ("Runtime", "runtime_error"),
    ):
        error = snapshot.get(key)
        if error:
            errors.append(f"{label}: {error}")

    if errors:
        lines.extend([
            "",
            "DIAGNOSTIC ERRORS",
            "----------------------------",
            *errors,
        ])

    return "\n".join(lines)[:3900]


@timed_handler
async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await deny_access(update)
        return

    worker_task = context.application.bot_data.get(
        WORKER_TASK_KEY
    )
    worker_running = (
        worker_task is not None
        and not worker_task.done()
    )

    with measure("status_command.collect_system_status"):
        snapshot = await asyncio.to_thread(
            collect_system_status,
            worker_running,
        )

    text = format_system_status(snapshot)

    with measure("status_command.reply_text"):
        await update.effective_message.reply_text(text)


# ============================================================
# REVIEW DETAIL
# ============================================================

async def send_review_detail(
    query,
    analysis_id,
):
    with measure("send_review_detail.get_review_detail.to_thread"):
        detail = await asyncio.to_thread(get_review_detail, analysis_id)

    # Manual TP sudah diambil di dalam get_review_detail (Session yang sama),
    # jadi tidak ada checkout koneksi tambahan di sini.
    tp_setting = detail.get("manual_tp")

    with measure("send_review_detail.edit_message_text"):
        await query.edit_message_text(
            review_detail_text(detail, tp_setting),
            reply_markup=review_detail_keyboard(analysis_id),
        )


# ============================================================
# TP HANDLING
# ============================================================

async def save_tp_from_callback(
    query,
    context,
    analysis_id,
    pips,
    user_id,
):
    result = await asyncio.to_thread(set_manual_tp, analysis_log_id=analysis_id, tp_pips=pips, user_id=user_id)

    distance = result["price_distance"]

    await query.edit_message_text(
        "MANUAL TP BERHASIL DISIMPAN\n"
        "============================\n\n"
        f"Analysis ID: #{analysis_id}\n"
        f"TP: {result['tp_pips']} pips\n"
        f"Jarak harga: {distance}\n"
        f"Status: {result['status']}\n\n"
        "TP mentor tidak diubah.\n"
        "TP ini belum diterapkan ke posisi MT5.\n\n"
        "Kamu bisa kembali ke detail analisis.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "BUKA DETAIL ANALISIS",
                    callback_data=f"review:{analysis_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "MENU UTAMA",
                    callback_data="menu",
                )
            ],
        ]),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

@timed_handler
async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_authorized(update):
        await deny_access(update)
        return

    with measure("callback_router.query.answer"):
        await query.answer()

    data = query.data or ""
    user_id = update.effective_user.id

    try:

        if data == "menu":
            await query.edit_message_text(
                await asyncio.to_thread(menu_text),
                reply_markup=menu_keyboard(),
            )
            return

        if data == "reviews":
            with measure("callback.reviews.list_pending_reviews.to_thread"):
                pending = await asyncio.to_thread(list_pending_reviews)

            with measure("callback.reviews.edit_message_text"):
                await query.edit_message_text(
                    review_list_text(pending),
                    reply_markup=review_list_keyboard(pending),
                )
            return

        if data == "mode:approval":
            state = await asyncio.to_thread(set_approval_mode, user_id)

            await query.edit_message_text(
                "APPROVAL MODE AKTIF\n\n"
                "Setiap sinyal membutuhkan keputusan manual.\n\n"
                f"Trading mode: {state['trading_mode']}\n"
                "Tidak ada order yang dikirim.",
                reply_markup=menu_keyboard(),
            )
            return

        if data == "mode:auto":
            await query.answer(
                "AUTO masih terkunci. Execution Engine belum siap.",
                show_alert=True,
            )
            return

        if data == "pause":
            state = await asyncio.to_thread(pause_auto, user_id)

            await query.edit_message_text(
                "AUTO PAUSED\n\n"
                "Eksekusi otomatis baru dinonaktifkan.\n"
                f"Execution mode: {state['execution_mode']}\n"
                "Tidak ada posisi MT5 yang ditutup oleh perintah ini.",
                reply_markup=menu_keyboard(),
            )
            return

        if data == "status":
            await query.edit_message_text(
                await asyncio.to_thread(menu_text),
                reply_markup=menu_keyboard(),
            )
            return

        parts = data.split(":")

        if parts[0] == "review" and len(parts) == 2:
            analysis_id = int(parts[1])
            await send_review_detail(query, analysis_id)
            return

        if parts[0] == "approve" and len(parts) == 2:
            analysis_id = int(parts[1])
            detail = await asyncio.to_thread(get_review_detail, analysis_id)

            if detail["status"] != "MANUAL_REVIEW":
                raise ValueError(
                    "Analisis ini tidak lagi menunggu review."
                )

            final = detail["final_analysis"]
            extracted = final.get("extracted") or {}
            uncertainties = final.get("uncertainties") or []

            uncertainty_text = (
                chr(10).join(f"- {item}" for item in uncertainties)
                if uncertainties else "- Tidak ada"
            )

            confirmation_text = chr(10).join([
                "?? KONFIRMASI APPROVAL",
                "============================",
                "",
                f"Analysis ID: #{analysis_id}",
                f"Telegram ID: {detail['target_message_id']}",
                f"Direction: {extracted.get('direction') or '-'}",
                f"Entry: {extracted.get('entry_zone_text') or '-'}",
                f"SL mentor: {extracted.get('stop_loss_text') or '-'}",
                f"TP mentor: {extracted.get('take_profit_texts') or 'Belum ada'}",
                f"Confidence: {final.get('confidence', '-')}",
                "",
                "Ketidakpastian:",
                uncertainty_text,
                "",
                "Jika dikonfirmasi, status review menjadi APPROVED.",
                "Ini BELUM mengirim order MT5.",
                "",
                "Lanjutkan persetujuan?",
            ])

            await query.edit_message_text(
                confirmation_text,
                reply_markup=approval_confirmation_keyboard(
                    analysis_id
                ),
            )
            return

        if parts[0] == "confirmapprove" and len(parts) == 2:
            analysis_id = int(parts[1])
            detail = await asyncio.to_thread(get_review_detail, analysis_id)

            if detail["status"] != "MANUAL_REVIEW":
                raise ValueError(
                    "Analisis sudah tidak menunggu review."
                )

            result = await asyncio.to_thread(decide_review, analysis_log_id=analysis_id, decision="APPROVED", reviewer=reviewer_name(update), note="Disetujui melalui konfirmasi Telegram.")

            await query.edit_message_text(
                "? REVIEW APPROVED" + chr(10) + chr(10)
                + f"Analysis ID: #{analysis_id}" + chr(10)
                + f"Reviewer: {result['reviewer']}" + chr(10) + chr(10)
                + "Keputusan berhasil disimpan." + chr(10)
                + "Tidak ada order MT5 yang dikirim.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "MENU UTAMA",
                            callback_data="menu",
                        )
                    ]
                ]),
            )
            return

        if parts[0] == "reject" and len(parts) == 2:
            analysis_id = int(parts[1])

            context.user_data["approval_action"] = {
                "type": "reject",
                "analysis_id": analysis_id,
            }

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Masukkan alasan penolakan untuk "
                    f"analisis #{analysis_id}."
                ),
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Alasan penolakan...",
                ),
            )
            return

        if parts[0] == "settp" and len(parts) == 2:
            analysis_id = int(parts[1])

            await query.edit_message_text(
                f"PENGATURAN TP — ANALISIS #{analysis_id}\n\n"
                "Standar SIREADER:\n"
                "1 pip = 0.10 perubahan harga.\n\n"
                "Pilih jumlah TP:",
                reply_markup=tp_keyboard(analysis_id),
            )
            return

        if parts[0] == "tp" and len(parts) == 3:
            analysis_id = int(parts[1])
            pips = parts[2]

            await save_tp_from_callback(
                query,
                context,
                analysis_id,
                pips,
                user_id,
            )
            return

        if parts[0] == "tpcustom" and len(parts) == 2:
            analysis_id = int(parts[1])

            context.user_data["approval_action"] = {
                "type": "tp",
                "analysis_id": analysis_id,
            }

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Masukkan TP dalam pips untuk analisis "
                    f"#{analysis_id}.\n\n"
                    "Contoh: 75\n"
                    "atau: 75.5\n\n"
                    "1 pip = 0.10 perubahan harga."
                ),
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Jumlah pips...",
                ),
            )
            return

    except (ValueError, LookupError) as error:
        await query.edit_message_text(
            f"Permintaan tidak berhasil:\n{error}",
            reply_markup=menu_keyboard(),
        )
    except BadRequest as error:
        if "Message is not modified" in str(error):
            logger.debug("Pesan Telegram sudah memiliki tampilan terbaru.")
            return

        logger.warning("Telegram menolak perubahan pesan: %s", error)
        try:
            await query.answer(
                "Pesan tidak dapat diperbarui. Coba buka menu lagi.",
                show_alert=True,
            )
        except Exception:
            pass
        return
    except Exception:
        logger.exception("Callback gagal diproses")
        await query.edit_message_text(
            "Terjadi kesalahan internal. "
            "Tidak ada klaim bahwa transaksi berhasil.",
            reply_markup=menu_keyboard(),
        )


# ============================================================
# TEXT INPUT: REJECTION REASON AND CUSTOM TP
# ============================================================

@timed_handler
async def handle_user_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await deny_access(update)
        return

    pending = context.user_data.get("approval_action")

    if not pending:
        return

    raw_text = update.effective_message.text.strip()
    analysis_id = pending["analysis_id"]
    action_type = pending["type"]

    # Clear pending state before writing to the database.
    context.user_data.pop("approval_action", None)

    try:

        if action_type == "reject":
            result = await asyncio.to_thread(decide_review, analysis_log_id=analysis_id, decision="REJECTED", reviewer=reviewer_name(update), note=raw_text)

            await update.effective_message.reply_text(
                "REVIEW REJECTED\n\n"
                f"Analysis ID: #{analysis_id}\n"
                f"Alasan: {result['note']}\n"
                "Keputusan tersimpan di database.\n"
                "Tidak ada order MT5 yang dikirim.",
                reply_markup=menu_keyboard(),
            )
            return

        if action_type == "tp":
            result = await asyncio.to_thread(set_manual_tp, analysis_log_id=analysis_id, tp_pips=raw_text, user_id=update.effective_user.id)

            await update.effective_message.reply_text(
                "MANUAL TP BERHASIL DISIMPAN\n\n"
                f"Analysis ID: #{analysis_id}\n"
                f"TP: {result['tp_pips']} pips\n"
                f"Jarak harga: {result['price_distance']}\n"
                f"Status: {result['status']}\n\n"
                "Belum diterapkan ke posisi MT5.",
                reply_markup=menu_keyboard(),
            )
            return

    except (ValueError, LookupError) as error:
        await update.effective_message.reply_text(
            f"Input tidak berhasil: {error}\n"
            "Silakan buka kembali menu review."
        )
    except Exception:
        logger.exception("Input pengguna gagal diproses")
        await update.effective_message.reply_text(
            "Terjadi kesalahan internal."
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Terjadi error pada Telegram Bot.",
        exc_info=context.error,
    )

async def notification_worker(application):
    """
    Mengirim notifikasi MANUAL_REVIEW dari database outbox.

    Worker hanya mengirim pesan Telegram.
    Tidak melakukan eksekusi trading.
    """

    logger.info("Telegram notification outbox aktif.")

    # Notifikasi MANUAL_REVIEW berkaitan dengan sinyal trading yang
    # butuh keputusan manusia, jadi deteksi event baru diprioritaskan:
    # polling tetap 1 detik (tanpa exponential backoff). Target deteksi
    # <= 2 detik setelah outbox commit. LISTEN/NOTIFY tidak dipakai
    # sebagai mekanisme utama karena Supavisor session pooler dapat
    # memutus/merotasi koneksi dan NOTIFY bisa hilang diam-diam;
    # polling 1s adalah mekanisme utama yang stabil.
    IDLE_SLEEP = 1.0
    SLOW_SEND_WARN_SECONDS = 5.0

    while True:
        event = None

        try:
            event = await asyncio.to_thread(claim_next_notification)

            if event is None:
                await asyncio.sleep(IDLE_SLEEP)
                continue

            send_start = time.perf_counter()

            analysis_id = event["analysis_log_id"]

            detail = await asyncio.to_thread(get_review_detail, analysis_id)

            # Jangan mengirim review yang sudah diputuskan.
            if detail["status"] != "MANUAL_REVIEW":
                await asyncio.to_thread(mark_notification_skipped, event["id"], "Analisis sudah tidak menunggu review.")
                continue

            # Manual TP sudah termuat di detail (Session yang sama).
            tp_setting = detail.get("manual_tp")

            message = await application.bot.send_message(
                chat_id=event["recipient_chat_id"],
                text=review_detail_text(detail, tp_setting),
                reply_markup=review_detail_keyboard(analysis_id),
            )

            await asyncio.to_thread(mark_notification_sent, outbox_id=event["id"], telegram_message_id=message.message_id)

            send_elapsed = time.perf_counter() - send_start

            logger.info(
                "Notifikasi review terkirim. Analysis ID=%s durasi_detik=%.2f",
                analysis_id,
                send_elapsed,
            )

            # Pengiriman lambat (> 5 detik) dicatat sebagai warning
            # agar regresi latensi notifikasi terlihat.
            if send_elapsed > SLOW_SEND_WARN_SECONDS:
                logger.warning(
                    "Notifikasi review LAMBAT. Analysis ID=%s durasi_detik=%.2f",
                    analysis_id,
                    send_elapsed,
                )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            logger.exception(
                "Pengiriman notification outbox gagal."
            )

            if event is not None:
                try:
                    await asyncio.to_thread(mark_notification_failed, event["id"], str(error))
                except Exception:
                    logger.exception(
                        "Gagal mencatat status notification."
                    )

            await asyncio.sleep(5)


WORKER_TASK_KEY = "notification_worker_task"


def _start_notification_worker(application):
    """
    Menjalankan tepat satu notification worker.

    Idempoten: jika task sudah ada dan masih hidup, tidak membuat yang
    kedua. Mengembalikan task yang aktif.
    """
    existing = application.bot_data.get(WORKER_TASK_KEY)

    if existing is not None and not existing.done():
        logger.info("Notification worker sudah aktif; tidak membuat yang baru.")
        return existing

    task = asyncio.create_task(
        notification_worker(application),
        name="sireader-notification-outbox",
    )
    application.bot_data[WORKER_TASK_KEY] = task
    logger.info("Notification worker dimulai.")
    return task


async def post_init(application):
    """
    Menyiapkan notifikasi review lama yang belum pernah dimasukkan ke
    antrean, menampilkan antrean PENDING yang tertahan, lalu
    mengaktifkan worker.

    Worker hanya mengirim notifikasi PENDING. Status lama (SENT, FAILED,
    SKIPPED) tidak diubah di sini.
    """

    recipient = settings.TELEGRAM_APPROVER_USER_ID

    created = await asyncio.to_thread(ensure_pending_review_notifications, recipient)

    logger.info(
        "Notifikasi review lama yang masuk antrean: %s",
        created,
    )

    # Tampilkan antrean PENDING yang tertahan SEBELUM worker mulai.
    # Read-only: tidak menghapus atau mengubah status notifikasi lama.
    pending = await asyncio.to_thread(list_pending_notification_ids)

    logger.info("Notifikasi PENDING tertahan: %s", len(pending))
    for item in pending:
        logger.info(
            "  PENDING outbox_id=%s analysis_log_id=%s",
            item["outbox_id"],
            item["analysis_log_id"],
        )

    _start_notification_worker(application)

    # Spread monitor tetap nonaktif sampai timestamp MT5 tervalidasi.
    if getattr(settings, "TRADE_DECISION_WATCH_ENABLED", False):
        trade_decision_watch_worker.start_watch_worker(application)
        logger.info("Trade decision watch worker diaktifkan.")
    else:
        logger.info(
            "Trade decision watch worker NONAKTIF "
            "(TRADE_DECISION_WATCH_ENABLED=False)."
        )


async def post_stop(application):
    """
    Menghentikan worker notifikasi saat aplikasi berhenti.

    Hanya membatalkan task; TIDAK membuat worker baru.
    """

    task = application.bot_data.pop(WORKER_TASK_KEY, None)

    if task is not None and not task.done():
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Notification worker dihentikan.")

    # Aman dipanggil meskipun worker belum pernah dinyalakan.
    await trade_decision_watch_worker.stop_watch_worker(application)

# ============================================================
# APPLICATION
# ============================================================

def build_application():

    token = settings.TELEGRAM_BOT_TOKEN
    approver_id = settings.TELEGRAM_APPROVER_USER_ID

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum dikonfigurasi."
        )

    if approver_id is None or approver_id <= 0:
        raise RuntimeError(
            "TELEGRAM_APPROVER_USER_ID belum valid."
        )

    # HTTPXRequest eksplisit: pool koneksi cukup agar polling (getUpdates),
    # handler, dan notification worker tidak rebutan satu koneksi.
    # Timeout eksplisit agar request yang macet tidak menggantung handler.
    # Saat profiling aktif, request class diganti subclass pengukur dan
    # httpx event hooks mengukur network+server per request. Pool size
    # TIDAK diubah oleh profiling.
    RequestClass = make_profiling_request_class()
    profiling_httpx_kwargs = httpx_event_hooks()
    extra_kwargs = (
        {"httpx_kwargs": {"event_hooks": profiling_httpx_kwargs}}
        if profiling_httpx_kwargs else {}
    )

    request = RequestClass(
        connection_pool_size=16,
        connect_timeout=10.0,
        read_timeout=15.0,
        write_timeout=15.0,
        pool_timeout=5.0,
        **extra_kwargs,
    )
    # getUpdates memakai koneksi long-poll tersendiri agar tidak menyita
    # pool request bot biasa.
    get_updates_request = RequestClass(
        connection_pool_size=2,
        connect_timeout=10.0,
        read_timeout=35.0,
        write_timeout=15.0,
        pool_timeout=5.0,
        **extra_kwargs,
    )

    application = (
        Application.builder()
        .token(token)
        # Update diproses konkuren. Aman karena:
        #  - decide_review memakai conditional UPDATE + cek rowcount
        #    (mencegah double decision pada analisis yang sama),
        #  - outbox memakai with_for_update(skip_locked) (atomic claim),
        #  - double approval ditolak di level database.
        # Urutan approval per-analisis tetap konsisten karena keputusan
        # bersifat idempoten di database, bukan bergantung urutan handler.
        .concurrent_updates(True)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("menu", menu_command)
    )

    application.add_handler(
        CommandHandler("reviews", reviews_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_user_text,
        )
    )

    application.add_error_handler(error_handler)

    return application


def main():
    print("SIREADER TELEGRAM BOT")
    print("============================")
    print("Mode awal: APPROVAL")
    print("Trading mode: DRY_RUN")
    print("AUTO: TERKUNCI")
    print("Order execution: TIDAK DIAKTIFKAN")
    print("Menunggu Telegram Bot API...")

    # Profiling read-only, aktif hanya jika env SIREADER_PROFILE=1.
    # Tidak mengubah business logic maupun konfigurasi pool.
    setup_file_logging()
    from app.database.connection import engine
    install_sqlalchemy_events(engine)

    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
