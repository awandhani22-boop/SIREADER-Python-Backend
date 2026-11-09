from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    # DATABASE
    DATABASE_URL: str

    # TELEGRAM
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: str
    TELEGRAM_TARGET_CHAT_ID: int
    # TELEGRAM APPROVAL BOT
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_APPROVER_USER_ID: int | None = None

    # EXECUTION CONTROL
    EXECUTION_MODE: str = "APPROVAL"
    AUTO_EXECUTION_ENABLED: bool = False

    # TRADING SAFETY
    TRADING_MODE: str = "DRY_RUN"
    REQUIRE_MANUAL_APPROVAL: bool = True
    TIMEZONE: str = "Asia/Jakarta"

    # READ-ONLY MT5 MARKET DATA
    # Kosong secara default: pipeline tidak akan mencoba koneksi MT5
    # sampai path terminal dan simbol broker diatur secara eksplisit.
    MT5_TERMINAL_PATH: str | None = None
    MT5_EXPECTED_SERVER: str | None = None
    MT5_SYMBOL: str | None = None
    MT5_MAX_TICK_AGE_SECONDS: int = 15

    # Spread decision watch worker.
    # Tetap OFF sampai validasi timestamp MT5 selesai.
    TRADE_DECISION_WATCH_ENABLED: bool = False

    # OMNIROUTE
    AI_PROVIDER: str = "omniroute"
    AI_API_KEY: str | None = None
    AI_BASE_URL: str = "http://127.0.0.1:20128/v1"
    AI_TIMEOUT_SECONDS: int = 120

    # AI MODEL 1: FAST
    AI_MODEL_FAST: str = (
        "tokenmurah/claude-haiku-4-5-20251001"
    )
    AI_MAX_OUTPUT_FAST: int = 350

    # AI MODEL 2: REASONING
    AI_MODEL_REASONING: str = (
        "tokenmurah/claude-sonnet-4-6"
    )
    AI_MAX_OUTPUT_REASONING: int = 1200

    # AI MODEL 3: ESCALATION
    AI_MODEL_ESCALATION: str = (
        "tokenmurah/claude-opus-5"
    )
    AI_MAX_OUTPUT_ESCALATION: int = 800

    # DAILY AI BUDGET
    AI_TARGET_DAILY_BUDGET_RP: int = 10000
    AI_HARD_DAILY_LIMIT_RP: int = 50000

    # ANTHROPIC USD BILLING (trial Claude Console melalui OmniRoute)
    # Budget IDR di atas tetap berlaku untuk provider TokenMurah.
    # Budget USD di bawah berlaku untuk model berprefix "anthropic/".
    AI_ANTHROPIC_MODEL_PREFIX: str = "anthropic/"
    AI_ANTHROPIC_MONTHLY_BUDGET_USD: Decimal = Decimal("5.00")
    AI_ANTHROPIC_WARNING_THRESHOLDS: str = "0.70,0.85,0.95"
    AI_ANTHROPIC_LONG_CONTEXT_TOKENS: int = 100000

    # Tarif USD per 1 juta token. Sumber: dokumentasi resmi Anthropic.
    # Haiku 5.5, prompt sampai 100.000 token.
    AI_ANTHROPIC_HAIKU_INPUT_USD: Decimal = Decimal("0.10")
    AI_ANTHROPIC_HAIKU_OUTPUT_USD: Decimal = Decimal("0.50")
    AI_ANTHROPIC_HAIKU_CACHE_WRITE_5M_USD: Decimal = Decimal("0.125")
    AI_ANTHROPIC_HAIKU_CACHE_WRITE_1H_USD: Decimal = Decimal("0.20")
    AI_ANTHROPIC_HAIKU_CACHE_READ_USD: Decimal = Decimal("0.01")

    # Haiku 5.5, prompt di atas 100.000 token (tarif long-context).
    AI_ANTHROPIC_HAIKU_LONG_INPUT_USD: Decimal = Decimal("0.50")
    AI_ANTHROPIC_HAIKU_LONG_OUTPUT_USD: Decimal = Decimal("2.50")
    AI_ANTHROPIC_HAIKU_LONG_CACHE_WRITE_5M_USD: Decimal = Decimal("0.625")
    AI_ANTHROPIC_HAIKU_LONG_CACHE_WRITE_1H_USD: Decimal = Decimal("1.00")
    AI_ANTHROPIC_HAIKU_LONG_CACHE_READ_USD: Decimal = Decimal("0.05")

    # Sonnet 5.5.
    AI_ANTHROPIC_SONNET_INPUT_USD: Decimal = Decimal("2.00")
    AI_ANTHROPIC_SONNET_OUTPUT_USD: Decimal = Decimal("10.00")
    AI_ANTHROPIC_SONNET_CACHE_WRITE_5M_USD: Decimal = Decimal("2.50")
    AI_ANTHROPIC_SONNET_CACHE_WRITE_1H_USD: Decimal = Decimal("4.00")
    AI_ANTHROPIC_SONNET_CACHE_READ_USD: Decimal = Decimal("0.10")

    # Opus 5.5.
    AI_ANTHROPIC_OPUS_INPUT_USD: Decimal = Decimal("4.00")
    AI_ANTHROPIC_OPUS_OUTPUT_USD: Decimal = Decimal("20.00")
    AI_ANTHROPIC_OPUS_CACHE_WRITE_5M_USD: Decimal = Decimal("5.00")
    AI_ANTHROPIC_OPUS_CACHE_WRITE_1H_USD: Decimal = Decimal("8.00")
    AI_ANTHROPIC_OPUS_CACHE_READ_USD: Decimal = Decimal("0.20")

    # COST CONTROL
    AI_MAX_RETRIES: int = 0
    AI_ENABLE_DEDUPLICATION: bool = True

    # AI PIPELINE
    AI_PIPELINE_ENABLED: bool = False
    AI_DEBOUNCE_SECONDS: float = 5.0

    # GENERIC WORKER RUNTIME — fail-closed defaults (Phase 4)
    WORKER_RUNTIME_ENABLED: bool = False
    WORKER_PROVIDER_MODE: str = "MOCK"
    WORKER_LIVE_PROVIDER_ENABLED: bool = False
    WORKER_MAX_TASK_COST_RP: Decimal = Decimal("0")
    WORKER_MAX_TASK_COST_USD: Decimal = Decimal("0")
    WORKER_TASK_TIMEOUT_SECONDS: int = 120
    WORKER_TASK_MAX_ATTEMPTS: int = 3
    WORKER_TASK_LEASE_SECONDS: int = 150
    WORKER_HEARTBEAT_TIMEOUT_SECONDS: int = 60

    # TOKENMURAH PRICING
    # Semua tarif dalam Rupiah per 1 juta token.

    # HAIKU 4.5
    AI_FAST_INPUT_PRICE: int = 528
    AI_FAST_OUTPUT_PRICE: int = 2640
    AI_FAST_CACHE_READ_PRICE: int = 53
    AI_FAST_CACHE_WRITE_PRICE: int = 660

    # SONNET 4.6
    AI_REASONING_INPUT_PRICE: int = 1346
    AI_REASONING_OUTPUT_PRICE: int = 6732
    AI_REASONING_CACHE_READ_PRICE: int = 135
    AI_REASONING_CACHE_WRITE_PRICE: int = 1683

    # OPUS 5
    AI_ESCALATION_INPUT_PRICE: int = 4749
    AI_ESCALATION_OUTPUT_PRICE: int = 23747
    AI_ESCALATION_CACHE_READ_PRICE: int = 475
    AI_ESCALATION_CACHE_WRITE_PRICE: int = 5937

    AI_MONTHLY_BUDGET_RP: int = 50000
    AI_BUDGET_PERIOD_DAYS: int = 30
    AI_FAST_MONTHLY_BUDGET_RP: int = 25000
    AI_REASONING_MONTHLY_BUDGET_RP: int = 22500
    AI_ESCALATION_MONTHLY_BUDGET_RP: int = 2500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )


settings = Settings()
