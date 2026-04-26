from __future__ import annotations

from dataclasses import is_dataclass, fields
from enum import Enum
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from adapter import MetaMaskConnectorApp
from siglume_api_sdk import ConnectedAccountRef, Environment, ExecutionContext, ExecutionKind


app = FastAPI(title="MetaMask Connector")
_adapter = MetaMaskConnectorApp()


def _env_from_process() -> Environment:
    value = str(os.environ.get("SIGLUME_ENV") or "").strip().lower()
    if value in {"live", "prod", "production"}:
        return Environment.LIVE
    if value in {"sandbox", "test", "testing", "dev", "development"}:
        return Environment.SANDBOX
    return Environment.SANDBOX


def _to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _to_jsonable(value.to_dict())
        except Exception:
            pass
    return str(value)


class InvokeConnectedAccount(BaseModel):
    provider_key: str
    session_token: str
    scopes: list[str] = Field(default_factory=list)
    environment: str | None = None


class InvokeRequest(BaseModel):
    input_params: dict[str, Any] = Field(default_factory=dict)
    execution_kind: str = "dry_run"
    connected_accounts: dict[str, InvokeConnectedAccount] = Field(default_factory=dict)


@app.get("/health")
async def health() -> JSONResponse:
    result = await _adapter.health_check()
    return JSONResponse(_to_jsonable(result))


@app.post("/invoke")
async def invoke(request: Request) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": "Request body must be a JSON object."},
        )

    # Accept both:
    # 1) {"input_params": {...}, "execution_kind": "...", "connected_accounts": {...}} (preferred)
    # 2) {"action": "...", ...} (treat as input_params; default execution_kind=dry_run)
    try:
        payload = InvokeRequest.model_validate(body)
    except ValidationError:
        payload = InvokeRequest(input_params=dict(body))

    kind_raw = str(payload.execution_kind or "").strip().lower()
    try:
        execution_kind = ExecutionKind(kind_raw)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_execution_kind",
                "message": "execution_kind must be one of: dry_run, action.",
                "provided": kind_raw,
            },
        )

    connected: dict[str, ConnectedAccountRef] = {}
    for key, account in (payload.connected_accounts or {}).items():
        provider_key = str(account.provider_key or key).strip() or str(key).strip()
        connected[str(key)] = ConnectedAccountRef(
            provider_key=provider_key,
            session_token=str(account.session_token),
            scopes=list(account.scopes or []),
            environment=_env_from_process(),
        )

    ctx = ExecutionContext(
        agent_id="http-runtime",
        owner_user_id="http-runtime",
        task_type="metamask_rpc",
        environment=_env_from_process(),
        execution_kind=execution_kind,
        input_params=dict(payload.input_params or {}),
        connected_accounts=connected,
        trace_id=str(request.headers.get("X-Trace-Id") or "").strip() or None,
        idempotency_key=str(request.headers.get("Idempotency-Key") or "").strip() or None,
    )
    result = await _adapter.execute(ctx)
    output = dict(getattr(result, "output", {}) or {})
    merged = {
        **output,
        "success": bool(getattr(result, "success", False)),
        "execution_kind": getattr(getattr(result, "execution_kind", None), "value", getattr(result, "execution_kind", None)),
        "units_consumed": int(getattr(result, "units_consumed", 0) or 0),
    }
    return JSONResponse(_to_jsonable(merged))
