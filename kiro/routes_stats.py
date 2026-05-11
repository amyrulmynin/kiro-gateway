# -*- coding: utf-8 -*-

"""
FastAPI routes for Usage Statistics API.

Provides endpoints for querying gateway usage statistics.
All endpoints require the same PROXY_API_KEY authentication.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from loguru import logger

from kiro.config import PROXY_API_KEY
from kiro.usage_tracker import (
    get_stats_overview,
    get_stats_by_model,
    get_stats_by_day,
    get_stats_by_hour,
    get_recent_errors,
    get_latency_stats,
)


# --- Security scheme ---
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
x_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


async def verify_stats_api_key(
    auth_header: Optional[str] = Security(api_key_header),
    x_api_key: Optional[str] = Security(x_api_key_header),
) -> bool:
    """
    Verify API key for stats endpoints.

    Supports both Authorization: Bearer and x-api-key headers.

    Args:
        auth_header: Authorization header value
        x_api_key: x-api-key header value

    Returns:
        True if key is valid

    Raises:
        HTTPException: 401 if key is invalid or missing
    """
    if auth_header and auth_header == f"Bearer {PROXY_API_KEY}":
        return True
    if x_api_key and x_api_key == PROXY_API_KEY:
        return True

    logger.warning("Access attempt with invalid API key (stats endpoint)")
    raise HTTPException(status_code=401, detail="Invalid or missing API Key")


# --- Router ---
router = APIRouter(prefix="/api/stats", tags=["Statistics"], dependencies=[Depends(verify_stats_api_key)])


@router.get("/overview")
async def stats_overview():
    """
    Get overall usage statistics.

    Returns:
        Total requests, tokens, success rate, active models
    """
    logger.debug("Request to /api/stats/overview")
    return await get_stats_overview()


@router.get("/by-model")
async def stats_by_model(days: int = Query(default=30, ge=1, le=365)):
    """
    Get per-model breakdown.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of per-model statistics
    """
    logger.debug(f"Request to /api/stats/by-model (days={days})")
    return await get_stats_by_model(days=days)


@router.get("/by-day")
async def stats_by_day(days: int = Query(default=30, ge=1, le=365)):
    """
    Get daily usage time series.

    Args:
        days: Number of days to look back (default: 30)

    Returns:
        List of daily statistics
    """
    logger.debug(f"Request to /api/stats/by-day (days={days})")
    return await get_stats_by_day(days=days)


@router.get("/by-hour")
async def stats_by_hour(hours: int = Query(default=24, ge=1, le=168)):
    """
    Get hourly usage breakdown.

    Args:
        hours: Number of hours to look back (default: 24)

    Returns:
        List of hourly statistics
    """
    logger.debug(f"Request to /api/stats/by-hour (hours={hours})")
    return await get_stats_by_hour(hours=hours)


@router.get("/errors")
async def stats_errors(limit: int = Query(default=50, ge=1, le=500)):
    """
    Get recent failed requests.

    Args:
        limit: Maximum number of errors to return (default: 50)

    Returns:
        List of recent error records
    """
    logger.debug(f"Request to /api/stats/errors (limit={limit})")
    return await get_recent_errors(limit=limit)


@router.get("/latency")
async def stats_latency(days: int = Query(default=7, ge=1, le=90)):
    """
    Get latency percentiles per model.

    Args:
        days: Number of days to look back (default: 7)

    Returns:
        List of latency stats per model (p50, p90, p99, avg)
    """
    logger.debug(f"Request to /api/stats/latency (days={days})")
    return await get_latency_stats(days=days)
