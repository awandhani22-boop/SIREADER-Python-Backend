from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ============================================================
# TELEGRAM MESSAGES
# ============================================================

class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "telegram_message_id",
            name="uq_telegram_chat_message",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
    )

    telegram_message_id: Mapped[int] = mapped_column(
        BigInteger,
    )

    sender: Mapped[str | None] = mapped_column(
        String(255),
    )

    message_text: Mapped[str | None] = mapped_column(
        Text,
    )

    reply_to_id: Mapped[int | None] = mapped_column(
        BigInteger,
    )

    is_edited: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    # Waktu pesan diterima dan disimpan oleh SIREADER.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Waktu asli pesan dikirim oleh mentor di Telegram.
    telegram_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


# ============================================================
# SIGNAL SETUPS (LEGACY COMPATIBILITY)
# ============================================================

class SignalSetup(Base):
    __tablename__ = "signal_setups"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    direction: Mapped[str | None] = mapped_column(
        String(10),
    )

    entry_low: Mapped[float | None] = mapped_column(
        Float,
    )

    entry_high: Mapped[float | None] = mapped_column(
        Float,
    )

    entry_price: Mapped[float | None] = mapped_column(
        Float,
    )

    stop_loss: Mapped[float | None] = mapped_column(
        Float,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="RECEIVED",
    )

    ai_confidence: Mapped[float | None] = mapped_column(
        Float,
    )

    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_messages.id"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# ============================================================
# PERSISTENT TRADE EPISODES
# ============================================================

class TradeEpisode(Base):
    __tablename__ = "trade_episodes"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # ID stabil: trade_{chat_id}_{telegram_message_id}.
    episode_key: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        index=True,
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        nullable=False,
    )

    # ID internal dari pesan Telegram yang membentuk zona/sinyal.
    anchor_message_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id"),
        unique=True,
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    entry_low: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    entry_high: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    entry_midpoint: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    # OPEN atau SKIP_REQUESTED.
    # Status CLOSED harus berasal dari verifikasi MT5.
    status: Mapped[str] = mapped_column(
        String(30),
        default="OPEN",
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================
# PERSISTENT TRADE DECISION WATCHES
# ============================================================

class TradeDecisionWatch(Base):
    __tablename__ = "trade_decision_watches"

    __table_args__ = (
        UniqueConstraint(
            "analysis_log_id",
            name="uq_trade_decision_watch_analysis",
        ),
        Index(
            "ix_trade_decision_watches_status_next_check",
            "status",
            "next_check_at",
        ),
        Index(
            "ix_trade_decision_watches_episode_status",
            "trade_episode_id",
            "status",
        ),
        CheckConstraint(
            "status IN ("
            "'WAITING_FOR_SPREAD', "
            "'RECHECKING', "
            "'RESOLVED', "
            "'SKIPPED', "
            "'CANCELLED', "
            "'EXPIRED', "
            "'ERROR'"
            ")",
            name="ck_trade_decision_watch_status",
        ),
        CheckConstraint(
            "maximum_spread_pips > 0",
            name="ck_trade_decision_watch_max_spread_positive",
        ),
        CheckConstraint(
            "spread_pips IS NULL OR spread_pips >= 0",
            name="ck_trade_decision_watch_spread_nonnegative",
        ),
        CheckConstraint(
            "check_count >= 0",
            name="ck_trade_decision_watch_check_count",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # Satu hasil analisis hanya memiliki satu watch.
    analysis_log_id: Mapped[int] = mapped_column(
        ForeignKey(
            "ai_analysis_logs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # FK menggunakan ID numerik TradeEpisode.
    # Bukan episode_key yang berbentuk string.
    trade_episode_id: Mapped[int] = mapped_column(
        ForeignKey(
            "trade_episodes.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    symbol: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    broker_server: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    # WAITING_FOR_SPREAD, RECHECKING, RESOLVED,
    # SKIPPED, CANCELLED, EXPIRED, ERROR.
    status: Mapped[str] = mapped_column(
        String(32),
        default="WAITING_FOR_SPREAD",
        nullable=False,
    )

    spread_pips: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    maximum_spread_pips: Mapped[Decimal] = mapped_column(
        Numeric(18, 5),
        nullable=False,
    )

    last_bid: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    last_ask: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    check_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# TRADE EPISODE MESSAGE MAPPING
# ============================================================

class TradeEpisodeMessage(Base):
    __tablename__ = "trade_episode_messages"

    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "telegram_message_db_id",
            name="uq_episode_telegram_message",
        ),
        Index(
            "uq_trade_episode_message_single_owner",
            "telegram_message_db_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    episode_id: Mapped[int] = mapped_column(
        ForeignKey("trade_episodes.id"),
        index=True,
        nullable=False,
    )

    # Mengarah ke telegram_messages.id, bukan ID Telegram eksternal.
    telegram_message_db_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id"),
        index=True,
        nullable=False,
    )

    # NEW_SIGNAL, ENTRY_REQUEST, ENTRY_CONDITION, STOP_LOSS,
    # TAKE_PROFIT, POSITION_UPDATE, SKIP, dan sebagainya.
    message_role: Mapped[str] = mapped_column(
        String(40),
        default="UNKNOWN",
        nullable=False,
    )

    # ANCHOR, ENTRY_REQUEST, ENTRY_CONFIRMATION, CONTEXT,
    # SKIP_REQUEST, POSITION_UPDATE, dan sebagainya.
    relation_type: Mapped[str] = mapped_column(
        String(40),
        default="CONTEXT",
        nullable=False,
    )

    # SAME_WINDOW, REPLY_TO, PERSISTENT_LOOKUP, MANUAL, ANCHOR.
    association_method: Mapped[str] = mapped_column(
        String(40),
        default="SAME_WINDOW",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================
# PERSISTENT PENDING ENTRY REQUESTS
# ============================================================

class TradePendingEntry(Base):
    __tablename__ = "trade_pending_entries"

    __table_args__ = (
        UniqueConstraint(
            "source_message_id",
            name="uq_trade_pending_source_message",
        ),
        Index(
            "ix_trade_pending_chat_direction_status",
            "chat_id",
            "direction",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    # Pesan mentor yang berisi Try BUY / Try SELL.
    source_message_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id"),
        nullable=False,
    )

    direction: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    # WAITING_FOR_ZONE, MATCHED, CANCELLED, EXPIRED, AMBIGUOUS.
    status: Mapped[str] = mapped_column(
        String(30),
        default="WAITING_FOR_ZONE",
        nullable=False,
        index=True,
    )

    # Timestamp Telegram dari pesan request, bukan waktu diproses.
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    matched_episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_episodes.id"),
        nullable=True,
        index=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================
# TELEGRAM MEDIA
# ============================================================

class TelegramMedia(Base):
    __tablename__ = "telegram_media"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id"),
        unique=True,
    )

    media_type: Mapped[str] = mapped_column(
        String(30),
    )

    file_path: Mapped[str] = mapped_column(
        Text,
    )

    mime_type: Mapped[str | None] = mapped_column(
        String(100),
    )

    file_size: Mapped[int | None] = mapped_column(
        BigInteger,
    )

    original_filename: Mapped[str | None] = mapped_column(
        String(255),
    )

    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# ============================================================
# TELEGRAM MESSAGE EDIT HISTORY
# ============================================================

class TelegramMessageEdit(Base):
    __tablename__ = "telegram_message_edits"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_messages.id"),
        index=True,
    )

    old_text: Mapped[str | None] = mapped_column(
        Text,
    )

    new_text: Mapped[str | None] = mapped_column(
        Text,
    )

    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


# ============================================================
# AI USAGE AND COST TRACKING
# ============================================================

class AIUsageLog(Base):
    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # ID unik setiap permintaan AI.
    request_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )

    # Tanggal penggunaan berdasarkan waktu Asia/Jakarta.
    usage_date: Mapped[date] = mapped_column(
        Date,
        index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
    )

    model: Mapped[str] = mapped_column(
        String(255),
    )

    # FAST, REASONING, atau ESCALATION.
    tier: Mapped[str] = mapped_column(
        String(30),
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    cache_read_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    cache_write_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    # Rincian TTL cache Anthropic (billing USD).
    cache_write_5m_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    cache_write_1h_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    # Thinking token adalah bagian dari output_tokens; metadata saja.
    thinking_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    # True bila TTL cache tidak diketahui dan ditagih dengan tarif write 1 jam.
    cache_ttl_estimated: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    # IDR untuk TokenMurah, USD untuk Anthropic.
    billing_currency: Mapped[str] = mapped_column(
        String(3),
        default="IDR",
    )

    # IDR: biaya aktual yang sudah diketahui.
    cost_rp: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
    )

    # IDR: estimasi biaya yang dicadangkan sebelum request.
    reserved_rp: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
    )

    # USD: terpisah penuh dari nilai IDR.
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
    )

    reserved_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
    )

    # Tautan opsional ke ai_analysis_logs.id untuk laporan per analysis.
    analysis_log_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    # RESERVED, COMPLETED, FAILED, atau UNKNOWN.
    status: Mapped[str] = mapped_column(
        String(20),
        default="RESERVED",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# AI ANALYSIS HISTORY
# ============================================================

class AIAnalysisLog(Base):
    __tablename__ = "ai_analysis_logs"

    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "target_message_id",
            name="uq_ai_analysis_chat_target",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
    )

    target_message_id: Mapped[int] = mapped_column(
        BigInteger,
    )

    # PROCESSING, ANALYZED, NO_SIGNAL, MANUAL_REVIEW, FAILED, dll.
    status: Mapped[str] = mapped_column(
        String(40),
        default="PROCESSING",
    )

    # Hasil JSON lengkap dari AI Router.
    result_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# MANUAL APPROVAL AUDIT
# ============================================================

class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"

    __table_args__ = (
        UniqueConstraint(
            "analysis_log_id",
            name="uq_approval_decision_analysis",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    analysis_log_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analysis_logs.id"),
        nullable=False,
        index=True,
    )

    previous_status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    decision: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    new_status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    reviewer: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================
# SIREADER RUNTIME CONTROL
# ============================================================

class RuntimeControlState(Base):
    __tablename__ = "runtime_control_state"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        default=1,
    )

    execution_mode: Mapped[str] = mapped_column(
        String(20),
        default="APPROVAL",
        nullable=False,
    )

    trading_mode: Mapped[str] = mapped_column(
        String(20),
        default="DRY_RUN",
        nullable=False,
    )

    auto_execution_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    auto_paused: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    updated_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================
# MANUAL TAKE PROFIT SETTINGS
# ============================================================

class ManualTPSetting(Base):
    __tablename__ = "manual_tp_settings"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    analysis_log_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analysis_logs.id"),
        index=True,
        nullable=False,
    )

    symbol: Mapped[str] = mapped_column(
        String(30),
        default="XAUUSD",
        nullable=False,
    )

    tp_pips: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    price_distance: Mapped[Decimal] = mapped_column(
        Numeric(18, 5),
        nullable=False,
    )

    set_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="WAITING_FOR_EXECUTION",
        nullable=False,
    )

    mt5_position_ticket: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    calculated_tp_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 5),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# TELEGRAM NOTIFICATION OUTBOX
# ============================================================

class TelegramOutbox(Base):
    __tablename__ = "telegram_notification_outbox"

    __table_args__ = (
        UniqueConstraint(
            "event_key",
            name="uq_telegram_outbox_event",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    event_key: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    analysis_log_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analysis_logs.id"),
        nullable=False,
        index=True,
    )

    recipient_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # PENDING, SENDING, SENT, FAILED, SKIPPED.
    status: Mapped[str] = mapped_column(
        String(20),
        default="PENDING",
        nullable=False,
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    telegram_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# AI BUDGET ALERTS
# ============================================================

class AIUsageAlert(Base):
    __tablename__ = "ai_usage_alerts"

    __table_args__ = (
        UniqueConstraint(
            "billing_currency",
            "period_start",
            "threshold_percent",
            name="uq_ai_usage_alert_period_threshold",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # IDR atau USD.
    billing_currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    # Awal jendela budget; dedup alert per siklus budget.
    period_start: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    # 70, 85, atau 95.
    threshold_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    budget_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
    )

    spent_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        nullable=False,
    )

    percent_used: Mapped[Decimal] = mapped_column(
        Numeric(9, 4),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # NULL berarti alert belum dikirim ke Telegram.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================
# AI PENDING MESSAGE QUEUE
# ============================================================

class AIPendingMessage(Base):
    __tablename__ = "ai_pending_messages"

    __table_args__ = (
        UniqueConstraint(
            "dedup_key",
            name="uq_ai_pending_dedup",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # chat_id:telegram_message_id — mencegah pesan sama masuk antrean dua kali.
    dedup_key: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    telegram_message_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    analysis_log_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    # PENDING, CLAIMED, ANALYZED, DISCARDED.
    status: Mapped[str] = mapped_column(
        String(20),
        default="PENDING",
        nullable=False,
        index=True,
    )

    # Alasan masuk antrean, mis. BLOCKED_HARD_BUDGET.
    reason: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    claimed_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
