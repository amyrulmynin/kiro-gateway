# -*- coding: utf-8 -*-

"""
Usage Tracker for Kiro Gateway.

Tracks API request statistics using SQLite database.
Provides async-safe operations using asyncio.to_thread for SQLite writes.
"""

import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger


# Database path from environment variable
USAGE_DB_PATH: str = os.getenv("USAGE_DB_PATH", "/app/data/usage.db")


def _get_db_path() -> str:
    """Get database path, creating parent directories if needed."""
    db_path = USAGE_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def _init_db(db_path: str) -> None:
    """
    Initialize database schema.

    Creates tables if they don't exist:
    - requests: Individual request records
    - daily_stats: Materialized daily aggregates

    Args:
        db_path: Path to SQLite database file
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                model TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms REAL NOT NULL DEFAULT 0,
                status_code INTEGER NOT NULL DEFAULT 200,
                error_message TEXT,
                account_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
            CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status_code);

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT NOT NULL,
                model TEXT NOT NULL,
                total_requests INTEGER NOT NULL DEFAULT 0,
                total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                total_completion_tokens INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, model)
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _insert_request(
    db_path: str,
    model: str,
    endpoint: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    status_code: int,
    error_message: Optional[str],
    account_id: Optional[str],
) -> None:
    """
    Insert a request record and update daily_stats.

    Args:
        db_path: Path to SQLite database
        model: Model name used
        endpoint: API endpoint (openai/anthropic)
        prompt_tokens: Number of prompt tokens
        completion_tokens: Number of completion tokens
        latency_ms: Request latency in milliseconds
        status_code: HTTP status code
        error_message: Error message if failed
        account_id: Account ID used
    """
    total_tokens = prompt_tokens + completion_tokens
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO requests 
               (timestamp, model, endpoint, prompt_tokens, completion_tokens, total_tokens, 
                latency_ms, status_code, error_message, account_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, model, endpoint, prompt_tokens, completion_tokens, total_tokens,
             latency_ms, status_code, error_message, account_id)
        )

        # Update daily_stats (upsert)
        is_success = 1 if 200 <= status_code < 400 else 0
        is_error = 1 if status_code >= 400 else 0

        conn.execute(
            """INSERT INTO daily_stats (date, model, total_requests, total_prompt_tokens, 
               total_completion_tokens, success_count, error_count)
               VALUES (?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(date, model) DO UPDATE SET
                   total_requests = total_requests + 1,
                   total_prompt_tokens = total_prompt_tokens + excluded.total_prompt_tokens,
                   total_completion_tokens = total_completion_tokens + excluded.total_completion_tokens,
                   success_count = success_count + excluded.success_count,
                   error_count = error_count + excluded.error_count""",
            (date_str, model, prompt_tokens, completion_tokens, is_success, is_error)
        )

        conn.commit()
    finally:
        conn.close()


def _query_stats_overview(db_path: str) -> Dict[str, Any]:
    """Query overall statistics."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT 
                COUNT(*) as total_requests,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(CASE WHEN status_code >= 200 AND status_code < 400 THEN 1 ELSE 0 END), 0) as success_count,
                COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) as error_count
               FROM requests"""
        ).fetchone()

        total = row["total_requests"]
        success_rate = (row["success_count"] / total * 100) if total > 0 else 0

        models_row = conn.execute(
            "SELECT COUNT(DISTINCT model) as count FROM requests"
        ).fetchone()

        return {
            "total_requests": total,
            "total_prompt_tokens": row["total_prompt_tokens"],
            "total_completion_tokens": row["total_completion_tokens"],
            "total_tokens": row["total_tokens"],
            "success_count": row["success_count"],
            "error_count": row["error_count"],
            "success_rate": round(success_rate, 2),
            "active_models": models_row["count"],
        }
    finally:
        conn.close()


def _query_stats_by_model(db_path: str, days: int) -> List[Dict[str, Any]]:
    """Query per-model breakdown for last N days."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT 
                model,
                SUM(total_requests) as total_requests,
                SUM(total_prompt_tokens) as total_prompt_tokens,
                SUM(total_completion_tokens) as total_completion_tokens,
                SUM(success_count) as success_count,
                SUM(error_count) as error_count
               FROM daily_stats
               WHERE date >= date('now', ?)
               GROUP BY model
               ORDER BY total_requests DESC""",
            (f"-{days} days",)
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_stats_by_day(db_path: str, days: int) -> List[Dict[str, Any]]:
    """Query daily time series for last N days."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT 
                date,
                SUM(total_requests) as total_requests,
                SUM(total_prompt_tokens) as total_prompt_tokens,
                SUM(total_completion_tokens) as total_completion_tokens,
                SUM(success_count) as success_count,
                SUM(error_count) as error_count
               FROM daily_stats
               WHERE date >= date('now', ?)
               GROUP BY date
               ORDER BY date ASC""",
            (f"-{days} days",)
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_stats_by_hour(db_path: str, hours: int) -> List[Dict[str, Any]]:
    """Query hourly breakdown for last N hours."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT 
                strftime('%Y-%m-%d %H:00', timestamp) as hour,
                COUNT(*) as total_requests,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(CASE WHEN status_code >= 200 AND status_code < 400 THEN 1 ELSE 0 END), 0) as success_count,
                COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) as error_count
               FROM requests
               WHERE timestamp >= datetime('now', ?)
               GROUP BY hour
               ORDER BY hour ASC""",
            (f"-{hours} hours",)
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_recent_errors(db_path: str, limit: int) -> List[Dict[str, Any]]:
    """Query recent failed requests."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT 
                id, timestamp, model, endpoint, status_code, 
                error_message, account_id, latency_ms
               FROM requests
               WHERE status_code >= 400
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _query_latency_stats(db_path: str, days: int) -> List[Dict[str, Any]]:
    """Query latency percentiles per model for last N days."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Get distinct models with requests in the period
        models = conn.execute(
            """SELECT DISTINCT model FROM requests
               WHERE timestamp >= datetime('now', ?) AND status_code >= 200 AND status_code < 400""",
            (f"-{days} days",)
        ).fetchall()

        results = []
        for model_row in models:
            model = model_row["model"]
            # Get all latencies for this model, sorted
            latencies = conn.execute(
                """SELECT latency_ms FROM requests
                   WHERE model = ? AND timestamp >= datetime('now', ?) 
                   AND status_code >= 200 AND status_code < 400
                   ORDER BY latency_ms ASC""",
                (model, f"-{days} days")
            ).fetchall()

            if not latencies:
                continue

            values = [r["latency_ms"] for r in latencies]
            n = len(values)

            def percentile(data: List[float], p: float) -> float:
                idx = int(p / 100 * (len(data) - 1))
                return round(data[idx], 1)

            results.append({
                "model": model,
                "count": n,
                "p50": percentile(values, 50),
                "p90": percentile(values, 90),
                "p99": percentile(values, 99),
                "avg": round(sum(values) / n, 1),
            })

        return results
    finally:
        conn.close()


# ==============================================================================
# Public Async API
# ==============================================================================

_db_initialized = False


def _ensure_db() -> str:
    """Ensure database is initialized, return path."""
    global _db_initialized
    db_path = _get_db_path()
    if not _db_initialized:
        _init_db(db_path)
        _db_initialized = True
        logger.info(f"Usage tracker database initialized at: {db_path}")
    return db_path


async def track_request(
    model: str,
    endpoint: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0,
    status_code: int = 200,
    error_message: Optional[str] = None,
    account_id: Optional[str] = None,
) -> None:
    """
    Track an API request. Fire-and-forget, never raises.

    Args:
        model: Model name used
        endpoint: API endpoint (openai/anthropic)
        prompt_tokens: Number of prompt tokens
        completion_tokens: Number of completion tokens
        latency_ms: Request latency in milliseconds
        status_code: HTTP status code
        error_message: Error message if failed
        account_id: Account ID used
    """
    try:
        db_path = _ensure_db()
        await asyncio.to_thread(
            _insert_request,
            db_path, model, endpoint, prompt_tokens, completion_tokens,
            latency_ms, status_code, error_message, account_id
        )
    except Exception as e:
        logger.warning(f"Usage tracking failed (non-fatal): {e}")


async def get_stats_overview() -> Dict[str, Any]:
    """
    Get overall statistics.

    Returns:
        Dict with total_requests, total_tokens, success_rate, active_models
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_stats_overview, db_path)


async def get_stats_by_model(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get per-model breakdown for last N days.

    Args:
        days: Number of days to look back

    Returns:
        List of per-model statistics
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_stats_by_model, db_path, days)


async def get_stats_by_day(days: int = 30) -> List[Dict[str, Any]]:
    """
    Get daily time series for last N days.

    Args:
        days: Number of days to look back

    Returns:
        List of daily statistics
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_stats_by_day, db_path, days)


async def get_stats_by_hour(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Get hourly breakdown for last N hours.

    Args:
        hours: Number of hours to look back

    Returns:
        List of hourly statistics
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_stats_by_hour, db_path, hours)


async def get_recent_errors(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get recent failed requests.

    Args:
        limit: Maximum number of errors to return

    Returns:
        List of recent error records
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_recent_errors, db_path, limit)


async def get_latency_stats(days: int = 7) -> List[Dict[str, Any]]:
    """
    Get latency percentiles per model.

    Args:
        days: Number of days to look back

    Returns:
        List of latency stats per model (p50, p90, p99, avg)
    """
    db_path = _ensure_db()
    return await asyncio.to_thread(_query_latency_stats, db_path, days)
