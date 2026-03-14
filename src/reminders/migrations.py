"""Database migrations for the reminders module."""
from __future__ import annotations

# T-002: migration v6 — reminders, suppression_rules, planner_log

MIGRATION_V6 = """
CREATE TABLE IF NOT EXISTS reminders (
    id              TEXT PRIMARY KEY,
    scheduled_time  TEXT NOT NULL,
    expires_at      TEXT,
    hint            TEXT NOT NULL,
    urgency         TEXT NOT NULL DEFAULT 'normal',
    context_notes   TEXT,
    source_event_id TEXT,
    source_type     TEXT NOT NULL DEFAULT 'calendar',
    reminder_type   TEXT NOT NULL DEFAULT 'primary',
    trigger_day     TEXT NOT NULL,
    reason          TEXT,
    sent            INTEGER NOT NULL DEFAULT 0,
    sent_at         TEXT,
    failed          INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    snooze_until    TEXT,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    expired         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression_rules (
    id            TEXT PRIMARY KEY,
    topic         TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'permanent',
    expires_at    TEXT,
    trigger_count INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL,
    added_by      TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS planner_log (
    id                TEXT PRIMARY KEY,
    event_time        TEXT NOT NULL,
    action            TEXT NOT NULL,
    reminder_id       TEXT,
    source_event_id   TEXT,
    reason            TEXT,
    raw_llm_output    TEXT,
    notification_sent INTEGER,
    metadata          TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(scheduled_time, sent, cancelled, expired, snooze_until);

CREATE INDEX IF NOT EXISTS idx_reminders_dedup
    ON reminders(source_event_id, reminder_type, trigger_day);

CREATE INDEX IF NOT EXISTS idx_planner_log_time
    ON planner_log(event_time);

CREATE INDEX IF NOT EXISTS idx_suppressions_active
    ON suppression_rules(scope, expires_at);
"""
