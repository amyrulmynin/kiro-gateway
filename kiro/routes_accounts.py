# -*- coding: utf-8 -*-

"""
FastAPI routes for Account Management API.

Provides CRUD endpoints for managing Kiro accounts (credentials.json).
All endpoints require the same PROXY_API_KEY authentication.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import PROXY_API_KEY, ACCOUNTS_CONFIG_FILE, ACCOUNTS_STATE_FILE


# --- Security scheme (same as routes_stats.py) ---
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
x_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


async def verify_accounts_api_key(
    auth_header: Optional[str] = Security(api_key_header),
    x_api_key: Optional[str] = Security(x_api_key_header),
) -> bool:
    """
    Verify API key for accounts endpoints.

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

    logger.warning("Access attempt with invalid API key (accounts endpoint)")
    raise HTTPException(status_code=401, detail="Invalid or missing API Key")


# --- Pydantic Models ---
class AccountCreate(BaseModel):
    """Request body for creating a new account."""
    type: str = Field(..., description="Account type: refresh_token, json, or sqlite")
    refresh_token: Optional[str] = Field(None, description="Refresh token (for type=refresh_token)")
    path: Optional[str] = Field(None, description="File path (for type=json or type=sqlite)")
    profile_arn: Optional[str] = Field(None, description="AWS profile ARN")
    region: str = Field("us-east-1", description="AWS region")


class AccountUpdate(BaseModel):
    """Request body for updating an account."""
    refresh_token: Optional[str] = Field(None, description="New refresh token")
    path: Optional[str] = Field(None, description="New file path")
    profile_arn: Optional[str] = Field(None, description="AWS profile ARN")
    region: Optional[str] = Field(None, description="AWS region")
    enabled: Optional[bool] = Field(None, description="Enable/disable account")


# --- Helpers ---
def _load_credentials_file() -> List[Dict[str, Any]]:
    """Load credentials.json from disk."""
    creds_path = Path(ACCOUNTS_CONFIG_FILE).expanduser()
    if not creds_path.exists():
        return []
    with open(creds_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_credentials_file(credentials: List[Dict[str, Any]]) -> None:
    """Save credentials.json atomically (write tmp + rename)."""
    creds_path = Path(ACCOUNTS_CONFIG_FILE).expanduser()
    tmp_path = creds_path.with_suffix('.json.tmp')

    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(credentials, f, indent=2, ensure_ascii=False)

    # Atomic rename
    tmp_path.replace(creds_path)
    logger.info("credentials.json saved atomically")


def _load_state_file() -> Dict[str, Any]:
    """Load state.json from disk."""
    state_path = Path(ACCOUNTS_STATE_FILE)
    if not state_path.exists():
        return {}
    with open(state_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _generate_account_id(entry: Dict[str, Any], index: int) -> str:
    """Generate a stable account ID for a credential entry."""
    cred_type = entry.get("type")
    if cred_type == "refresh_token":
        token = entry.get("refresh_token", "")
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        return f"refresh_token_{token_hash}"
    elif cred_type in ("json", "sqlite"):
        path = entry.get("path", "")
        expanded = str(Path(path).expanduser().resolve())
        return expanded
    return f"account_{index}"


def _redact_token(token: str) -> str:
    """Redact refresh token, showing only first 20 chars."""
    if not token:
        return ""
    if len(token) <= 20:
        return token
    return token[:20] + "..."


def _get_account_status(account_id: str, state_data: Dict[str, Any]) -> Dict[str, Any]:
    """Get account status from state.json data."""
    accounts_state = state_data.get("accounts", {})
    account_state = accounts_state.get(account_id, {})

    failures = account_state.get("failures", 0)
    last_failure_time = account_state.get("last_failure_time", 0.0)
    stats = account_state.get("stats", {})

    if failures == 0:
        status = "active"
    elif failures >= 5:
        status = "broken"
    else:
        status = "degraded"

    return {
        "status": status,
        "failures": failures,
        "last_failure_time": last_failure_time,
        "total_requests": stats.get("total_requests", 0),
        "successful_requests": stats.get("successful_requests", 0),
        "failed_requests": stats.get("failed_requests", 0),
    }


# --- Router ---
router = APIRouter(
    prefix="/api/accounts",
    tags=["Accounts"],
    dependencies=[Depends(verify_accounts_api_key)]
)


@router.get("")
async def list_accounts(request: Request):
    """
    List all accounts with redacted tokens and status info.

    Returns:
        List of account objects with status, stats, and redacted credentials
    """
    credentials = _load_credentials_file()
    state_data = _load_state_file()

    result = []
    for idx, entry in enumerate(credentials):
        account_id = _generate_account_id(entry, idx)
        status_info = _get_account_status(account_id, state_data)

        account_info = {
            "id": idx,
            "account_id": account_id,
            "type": entry.get("type"),
            "enabled": entry.get("enabled", True),
            "region": entry.get("region", "us-east-1"),
            "profile_arn": entry.get("profile_arn", ""),
            **status_info,
        }

        # Redact sensitive fields
        if entry.get("type") == "refresh_token":
            account_info["refresh_token"] = _redact_token(entry.get("refresh_token", ""))
        elif entry.get("type") in ("json", "sqlite"):
            account_info["path"] = entry.get("path", "")

        result.append(account_info)

    return {"accounts": result, "total": len(result)}


@router.post("")
async def add_account(body: AccountCreate, request: Request):
    """
    Add a new account to credentials.json.

    Args:
        body: Account creation data

    Returns:
        Created account info
    """
    # Validate based on type
    if body.type == "refresh_token":
        if not body.refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token is required for type=refresh_token")
        new_entry = {
            "type": "refresh_token",
            "refresh_token": body.refresh_token,
            "region": body.region,
        }
        if body.profile_arn:
            new_entry["profile_arn"] = body.profile_arn

    elif body.type in ("json", "sqlite"):
        if not body.path:
            raise HTTPException(status_code=400, detail="path is required for type=json/sqlite")
        new_entry = {
            "type": body.type,
            "path": body.path,
            "region": body.region,
        }
        if body.profile_arn:
            new_entry["profile_arn"] = body.profile_arn
    else:
        raise HTTPException(status_code=400, detail=f"Invalid type: {body.type}. Must be refresh_token, json, or sqlite")

    # Load, append, save
    credentials = _load_credentials_file()
    credentials.append(new_entry)
    _save_credentials_file(credentials)

    # Trigger reload
    account_manager = request.app.state.account_manager
    await account_manager.reload()

    idx = len(credentials) - 1
    account_id = _generate_account_id(new_entry, idx)

    return {
        "message": "Account added successfully",
        "id": idx,
        "account_id": account_id,
        "type": new_entry["type"],
    }


@router.put("/{account_idx}")
async def update_account(account_idx: int, body: AccountUpdate, request: Request):
    """
    Update an existing account.

    Args:
        account_idx: Account index in credentials array
        body: Fields to update

    Returns:
        Updated account info
    """
    credentials = _load_credentials_file()

    if account_idx < 0 or account_idx >= len(credentials):
        raise HTTPException(status_code=404, detail="Account not found")

    entry = credentials[account_idx]

    # Update fields
    if body.refresh_token is not None and entry.get("type") == "refresh_token":
        entry["refresh_token"] = body.refresh_token
    if body.path is not None and entry.get("type") in ("json", "sqlite"):
        entry["path"] = body.path
    if body.profile_arn is not None:
        entry["profile_arn"] = body.profile_arn
    if body.region is not None:
        entry["region"] = body.region
    if body.enabled is not None:
        entry["enabled"] = body.enabled

    credentials[account_idx] = entry
    _save_credentials_file(credentials)

    # Trigger reload
    account_manager = request.app.state.account_manager
    await account_manager.reload()

    return {"message": "Account updated successfully", "id": account_idx}


@router.delete("/{account_idx}")
async def delete_account(account_idx: int, request: Request):
    """
    Delete an account from credentials.json.

    Args:
        account_idx: Account index in credentials array

    Returns:
        Confirmation message
    """
    credentials = _load_credentials_file()

    if account_idx < 0 or account_idx >= len(credentials):
        raise HTTPException(status_code=404, detail="Account not found")

    removed = credentials.pop(account_idx)
    _save_credentials_file(credentials)

    # Trigger reload
    account_manager = request.app.state.account_manager
    await account_manager.reload()

    return {"message": "Account deleted successfully", "type": removed.get("type")}


@router.post("/reload")
async def reload_accounts(request: Request):
    """
    Force reload credentials.json from disk.
    If gateway is in degraded mode, attempts to re-initialize accounts.

    Returns:
        Reload status with account count and degraded mode status
    """
    account_manager = request.app.state.account_manager
    await account_manager.reload()

    # Try to exit degraded mode by re-initializing accounts
    was_degraded = getattr(request.app.state, 'degraded_mode', False)
    if was_degraded:
        all_accounts = list(account_manager._accounts.keys())
        for account_id in all_accounts:
            success = await account_manager._initialize_account(account_id)
            if success:
                request.app.state.degraded_mode = False
                break

    credentials = _load_credentials_file()
    return {
        "message": "Accounts reloaded successfully",
        "total_accounts": len(credentials),
        "degraded_mode": getattr(request.app.state, 'degraded_mode', False),
    }


@router.post("/{account_idx}/reset")
async def reset_account(account_idx: int, request: Request):
    """
    Reset failure counter for an account.

    Args:
        account_idx: Account index in credentials array

    Returns:
        Confirmation message
    """
    credentials = _load_credentials_file()

    if account_idx < 0 or account_idx >= len(credentials):
        raise HTTPException(status_code=404, detail="Account not found")

    entry = credentials[account_idx]
    account_id = _generate_account_id(entry, account_idx)

    # Reset in account_manager
    account_manager = request.app.state.account_manager
    account = account_manager._accounts.get(account_id)
    if account:
        account.failures = 0
        account.last_failure_time = 0.0
        account_manager._dirty = True
        await account_manager._save_state()

    return {"message": "Account reset successfully", "account_id": account_id}


@router.get("/health")
async def accounts_health(request: Request):
    """
    Get per-account health information.

    Returns:
        Health status for each account including failure rate and last success
    """
    credentials = _load_credentials_file()
    state_data = _load_state_file()

    health_info = []
    for idx, entry in enumerate(credentials):
        account_id = _generate_account_id(entry, idx)
        status_info = _get_account_status(account_id, state_data)

        total = status_info["total_requests"]
        failed = status_info["failed_requests"]
        failure_rate = (failed / total * 100) if total > 0 else 0.0

        health_info.append({
            "id": idx,
            "account_id": account_id,
            "type": entry.get("type"),
            "enabled": entry.get("enabled", True),
            "status": status_info["status"],
            "failures": status_info["failures"],
            "total_requests": total,
            "successful_requests": status_info["successful_requests"],
            "failed_requests": failed,
            "failure_rate": round(failure_rate, 2),
            "last_failure_time": status_info["last_failure_time"],
        })

    return {"accounts": health_info}
