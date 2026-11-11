"""Safe process-local settings for worker unit tests; never load real service credentials."""
from __future__ import annotations

import os


# These values exist only inside the pytest process. They satisfy required app
# settings without opening a database or authenticating to Telegram.
# A syntactically valid loopback PostgreSQL URL; these unit tests replace all DB
# side effects and must never establish a connection to this intentionally unused DB.
os.environ["DATABASE_URL"] = "postgresql+psycopg://unit_test@127.0.0.1:5432/sireader_unit_test_unused"
os.environ["TELEGRAM_API_ID"] = "1"
os.environ["TELEGRAM_API_HASH"] = "unit-test-placeholder"
os.environ["TELEGRAM_TARGET_CHAT_ID"] = "-1000000000"
os.environ["WORKER_RUNTIME_ENABLED"] = "false"
os.environ["WORKER_PROVIDER_MODE"] = "MOCK"
os.environ["WORKER_LIVE_PROVIDER_ENABLED"] = "false"
os.environ["WORKER_MAX_TASK_COST_RP"] = "0"
os.environ["WORKER_MAX_TASK_COST_USD"] = "0"
os.environ.pop("AI_API_KEY", None)
os.environ.pop("OMNIROUTE_API_KEY", None)
