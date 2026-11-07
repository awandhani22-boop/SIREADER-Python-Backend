from app.database.models import Base, TradeDecisionWatch


def test_trade_decision_watch_table_registered():
    assert "trade_decision_watches" in Base.metadata.tables


def test_trade_decision_watch_required_columns():
    table = TradeDecisionWatch.__table__

    required = {
        "id",
        "analysis_log_id",
        "trade_episode_id",
        "symbol",
        "broker_server",
        "status",
        "spread_pips",
        "maximum_spread_pips",
        "last_bid",
        "last_ask",
        "last_checked_at",
        "next_check_at",
        "check_count",
        "last_reason",
        "created_at",
        "updated_at",
        "resolved_at",
    }

    assert required.issubset(set(table.columns.keys()))


def test_trade_decision_watch_foreign_keys():
    table = TradeDecisionWatch.__table__

    targets = {
        fk.target_fullname
        for column in table.columns
        for fk in column.foreign_keys
    }

    assert "ai_analysis_logs.id" in targets
    assert "trade_episodes.id" in targets


def test_trade_decision_watch_unique_analysis_constraint():
    table = TradeDecisionWatch.__table__

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("analysis_log_id",) in unique_columns


def test_trade_decision_watch_indexes():
    table = TradeDecisionWatch.__table__
    names = {index.name for index in table.indexes}

    assert "ix_trade_decision_watches_status_next_check" in names
    assert "ix_trade_decision_watches_episode_status" in names


def test_trade_decision_watch_check_constraints():
    table = TradeDecisionWatch.__table__
    names = {
        constraint.name
        for constraint in table.constraints
    }

    assert "ck_trade_decision_watch_status" in names
    assert "ck_trade_decision_watch_max_spread_positive" in names
    assert "ck_trade_decision_watch_spread_nonnegative" in names
    assert "ck_trade_decision_watch_check_count" in names
