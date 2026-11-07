from app.approval.telegram_bot import format_system_status


def test_status_report_menampilkan_runtime_dan_health():
    snapshot = {
        "telegram": "ONLINE (command received)",
        "notification_worker": "RUNNING",
        "database": "OK",
        "ai_pipeline": "MODULE LOADABLE",
        "outbox_pending": 0,
        "runtime": {
            "execution_mode": "APPROVAL",
            "trading_mode": "DRY_RUN",
            "auto_execution_enabled": False,
            "auto_paused": True,
        },
        "mt5_configured": True,
        "mt5_connection": "NOT CHECKED",
        "mt5_account": "UNKNOWN",
        "approver_configured": True,
    }

    output = format_system_status(snapshot)

    assert "SIREADER SYSTEM STATUS" in output
    assert "Execution mode: APPROVAL" in output
    assert "Trading mode: DRY_RUN" in output
    assert "Auto execution: False" in output
    assert "Auto paused: True" in output
    assert "Telegram Bot: ONLINE (command received)" in output
    assert "Database: OK" in output
    assert "AI Pipeline module: MODULE LOADABLE" in output
    assert "Notification Worker: RUNNING" in output
    assert "Outbox Pending: 0" in output
    assert "Connection: NOT CHECKED" in output
    assert "Account mode: UNKNOWN" in output
    assert "Connector policy: READ ONLY" in output
    assert "Order execution: DISABLED" in output
    assert "Order sent by status check: False" in output


def test_status_report_menggunakan_unknown_jika_data_tidak_tersedia():
    snapshot = {
        "telegram": "ONLINE (command received)",
        "notification_worker": "NOT RUNNING",
        "database": "ERROR",
        "ai_pipeline": "IMPORT ERROR",
        "outbox_pending": None,
        "runtime": None,
        "mt5_configured": False,
        "approver_configured": False,
        "database_error": "OperationalError",
    }

    output = format_system_status(snapshot)

    assert "Execution mode: UNKNOWN" in output
    assert "Trading mode: UNKNOWN" in output
    assert "Database: ERROR" in output
    assert "Outbox Pending: UNKNOWN" in output
    assert "Terminal configuration: INCOMPLETE" in output
    assert "DIAGNOSTIC ERRORS" in output
    assert "Database: OperationalError" in output
