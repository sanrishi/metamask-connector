from __future__ import annotations

import os
import re
from typing import Any

import httpx

from siglume_api_sdk import (
    AppAdapter,
    AppCategory,
    AppManifest,
    ApprovalMode,
    ConnectedAccountRef,
    ExecutionContext,
    ExecutionKind,
    ExecutionResult,
    HealthCheckResult,
    PermissionClass,
    PriceModel,
)


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


SUPPORTED_CHAINS: dict[int, str] = {
    1: "Ethereum Mainnet",
    5: "Goerli Testnet",
    11155111: "Sepolia Testnet",
    137: "Polygon",
    42161: "Arbitrum One",
}


class MetaMaskConnectorApp(AppAdapter):
    def manifest(self) -> AppManifest:
        return AppManifest(
            capability_key="metamask-connector",
            version="0.1.0",
            name="MetaMask Connector",
            job_to_be_done="Check Ethereum wallet balances, chain ID, and transaction receipts via MetaMask-connected accounts, with strict EIP-55 address validation.",
            category=AppCategory.FINANCE,
            permission_class=PermissionClass.READ_ONLY,
            approval_mode=ApprovalMode.AUTO,
            dry_run_supported=True,
            required_connected_accounts=["metamask"],
            permission_scopes=["wallet.balance", "wallet.read", "wallet.tx_status"],
            price_model=PriceModel.FREE,
            jurisdiction="US",
            short_description=(
                "Read-only Ethereum JSON-RPC lookups (chain id, balance, transaction receipt). "
                "No funds are custodied, no transactions are signed, and no value is transmitted."
            ),
            docs_url="https://github.com/sanrishi/metamask-connector",
            support_contact="https://github.com/sanrishi/metamask-connector/issues",
            example_prompts=[
                "What chain is my wallet connected to?",
                "Check the ETH balance for 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                "Look up the receipt for tx 0x...",
            ],
            compatibility_tags=["web3", "ethereum", "wallet", "metamask"],
        )

    def supported_task_types(self) -> list[str]:
        return ["metamask_rpc"]

    async def health_check(self) -> HealthCheckResult:
        rpc_url = _rpc_url_from_env()
        if not rpc_url:
            return HealthCheckResult(
                healthy=True,
                message="METAMASK_RPC_URL not set; using deterministic stub for local tests.",
                provider_status={"metamask": "stub"},
            )
        return HealthCheckResult(
            healthy=True,
            message="RPC configured.",
            provider_status={"metamask": "configured"},
        )

    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        if ctx.execution_kind in (ExecutionKind.QUOTE, ExecutionKind.PAYMENT):
            return ExecutionResult(
                success=False,
                execution_kind=ctx.execution_kind,
                provider_status="not_implemented",
                error_message="Unsupported execution kind for this read-only connector.",
                output={
                    "summary": "Unsupported execution kind for this read-only connector.",
                    "amount_usd": 0.0,
                    "currency": "USD",
                },
                receipt_summary={
                    "type": "error_receipt",
                    "error_code": "not_implemented",
                    "message": "Unsupported execution kind for this read-only connector.",
                    "details": {"execution_kind": str(ctx.execution_kind)},
                    "provider": "metamask",
                },
                needs_approval=False,
                units_consumed=1,
            )

        action = str(ctx.input_params.get("action") or "").strip().lower()
        if action in ("", "example", "default"):
            action = "chain_id"

        if action == "chain_id":
            return await self._handle_chain_id(ctx)
        if action == "balance":
            return await self._handle_balance(ctx)
        if action in ("tx_receipt", "receipt", "transaction_receipt"):
            return await self._handle_tx_receipt(ctx)

        return _ok_result(
            ctx,
            output={
                "summary": f"Unknown action '{action}'. Supported: chain_id, balance, tx_receipt.",
                "amount_usd": 0.0,
                "currency": "USD",
            },
            receipt_note="unknown_action",
        )

    async def _handle_chain_id(self, ctx: ExecutionContext) -> ExecutionResult:
        chain_id = await _eth_chain_id(ctx, connected=ctx.connected_accounts.get("metamask"))
        if chain_id.error is not None:
            return chain_id.error
        network = SUPPORTED_CHAINS.get(chain_id.value or 0, "Unknown")
        output = {
            "summary": f"Connected chain id is {chain_id.value} ({network}).",
            "chain_id": str(chain_id.value),
            "network": network,
            "amount_usd": 0.0,
            "currency": "USD",
        }
        if ctx.execution_kind == ExecutionKind.DRY_RUN:
            return ExecutionResult(success=True, execution_kind=ctx.execution_kind, output=output, units_consumed=1)
        return _ok_result(ctx, output=output, receipt_note="phase1_chain_id")

    async def _handle_balance(self, ctx: ExecutionContext) -> ExecutionResult:
        address_raw = str(ctx.input_params.get("address") or "").strip()
        address = _require_eip55_address(ctx, address_raw)
        if isinstance(address, ExecutionResult):
            return address

        expected_chain = _parse_optional_chain_id(ctx.input_params.get("expected_chain_id"))
        if expected_chain is not None:
            chain_id = await _eth_chain_id(ctx, connected=ctx.connected_accounts.get("metamask"))
            if chain_id.error is not None:
                return chain_id.error
            if chain_id.value != expected_chain:
                return _network_mismatch(ctx, expected=expected_chain, actual=chain_id.value)

        bal = await _eth_get_balance(ctx, address, connected=ctx.connected_accounts.get("metamask"))
        if bal.error is not None:
            return bal.error

        chain_id_val = bal.chain_id
        network = SUPPORTED_CHAINS.get(chain_id_val or 0, "Unknown") if chain_id_val is not None else "Unknown"
        output = {
            "summary": f"Balance for {address} on chain {chain_id_val} ({network}) is {bal.eth} ETH.",
            "chain_id": str(chain_id_val) if chain_id_val is not None else "",
            "network": network,
            "balance_wei": str(bal.wei),
            "balance_eth": bal.eth,
            "tx_receipt": {},
            "amount_usd": 0.0,
            "currency": "USD",
        }
        if ctx.execution_kind == ExecutionKind.DRY_RUN:
            return ExecutionResult(success=True, execution_kind=ctx.execution_kind, output=output, units_consumed=1)
        return _ok_result(ctx, output=output, receipt_note="phase1_balance")

    async def _handle_tx_receipt(self, ctx: ExecutionContext) -> ExecutionResult:
        tx_hash = str(ctx.input_params.get("tx_hash") or "").strip()
        if not _TX_HASH_RE.fullmatch(tx_hash):
            return _structured_error(
                ctx,
                error_code="invalid_tx_hash",
                message="tx_hash must be 0x-prefixed 32-byte hex (64 hex chars).",
                details={"field": "tx_hash", "provided": tx_hash},
            )

        expected_chain = _parse_optional_chain_id(ctx.input_params.get("expected_chain_id"))
        if expected_chain is not None:
            chain_id = await _eth_chain_id(ctx, connected=ctx.connected_accounts.get("metamask"))
            if chain_id.error is not None:
                return chain_id.error
            if chain_id.value != expected_chain:
                return _network_mismatch(ctx, expected=expected_chain, actual=chain_id.value)

        receipt = await _eth_get_transaction_receipt(ctx, tx_hash, connected=ctx.connected_accounts.get("metamask"))
        if receipt.error is not None:
            return receipt.error

        network = SUPPORTED_CHAINS.get(receipt.chain_id or 0, "Unknown") if receipt.chain_id is not None else "Unknown"
        output = {
            "summary": "Transaction receipt lookup completed.",
            "chain_id": str(receipt.chain_id) if receipt.chain_id is not None else "",
            "network": network,
            "balance_wei": "",
            "balance_eth": "",
            "tx_receipt": receipt.receipt or {},
            "amount_usd": 0.0,
            "currency": "USD",
        }
        if ctx.execution_kind == ExecutionKind.DRY_RUN:
            return ExecutionResult(success=True, execution_kind=ctx.execution_kind, output=output, units_consumed=1)
        return _ok_result(ctx, output=output, receipt_note="phase1_tx_receipt")


def _rpc_url_from_env() -> str:
    return str(os.environ.get("METAMASK_RPC_URL") or "").strip()


def _has_connected_metamask(connected: ConnectedAccountRef | None) -> bool:
    return connected is not None and bool(str(connected.session_token or "").strip())


def _parse_optional_chain_id(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.startswith(("0x", "0X")):
        try:
            return int(text, 16)
        except ValueError:
            return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _network_mismatch(ctx: ExecutionContext, *, expected: int, actual: int | None) -> ExecutionResult:
    return _structured_error(
        ctx,
        error_code="network_mismatch",
        message="RPC chain id does not match expected_chain_id.",
        details={
            "expected_chain_id": expected,
            "actual_chain_id": actual,
            "expected_network": SUPPORTED_CHAINS.get(expected, "Unknown"),
            "actual_network": SUPPORTED_CHAINS.get(actual or 0, "Unknown") if actual is not None else "Unknown",
        },
    )


def _require_eip55_address(ctx: ExecutionContext, address: str) -> str | ExecutionResult:
    if not address:
        return _structured_error(
            ctx,
            error_code="invalid_address",
            message="address is required for action=balance.",
            details={"field": "address", "provided": address},
        )
    if not _ADDRESS_RE.fullmatch(address):
        return _structured_error(
            ctx,
            error_code="invalid_address",
            message="address must be 0x-prefixed 20-byte hex (40 hex chars).",
            details={"field": "address", "provided": address},
        )

    checksummed = _to_checksum_address(address)
    if checksummed != address:
        return _structured_error(
            ctx,
            error_code="invalid_address",
            message="Strict EIP-55 checksum required for address.",
            details={
                "field": "address",
                "provided": address,
                "expected_eip55": checksummed
            },
        )
    return address


def _structured_error(
    ctx: ExecutionContext,
    *,
    error_code: str,
    message: str,
    details: dict[str, Any],
) -> ExecutionResult:
    receipt = {
        "type": "error_receipt",
        "error_code": error_code,
        "message": message,
        "details": details,
        "execution_kind": getattr(ctx.execution_kind, "value", str(ctx.execution_kind)),
        "phase": "phase1",
        "provider": "metamask",
    }
    return ExecutionResult(
        success=False,
        execution_kind=ctx.execution_kind,
        error_message=message,
        provider_status="error",
        output={"summary": message, "amount_usd": 0.0, "currency": "USD"},
        receipt_summary=receipt,
        units_consumed=1,
        needs_approval=False,
    )


def _ok_result(ctx: ExecutionContext, *, output: dict[str, Any], receipt_note: str) -> ExecutionResult:
    return ExecutionResult(
        success=True,
        execution_kind=ctx.execution_kind,
        output=output,
        units_consumed=1,
        needs_approval=False,
        receipt_summary={
            "type": "receipt",
            "note": receipt_note,
            "phase": "phase1",
            "provider": "metamask",
            "execution_kind": getattr(ctx.execution_kind, "value", str(ctx.execution_kind)),
        },
        side_effects=[],
        artifacts=[],
    )


class _ChainIdResult:
    def __init__(self, value: int | None, error: ExecutionResult | None = None):
        self.value = value
        self.error = error


class _BalanceResult:
    def __init__(
        self,
        wei: int,
        eth: str,
        chain_id: int | None,
        error: ExecutionResult | None = None,
    ):
        self.wei = wei
        self.eth = eth
        self.chain_id = chain_id
        self.error = error


class _ReceiptResult:
    def __init__(
        self,
        receipt: dict[str, Any] | None,
        chain_id: int | None,
        error: ExecutionResult | None = None,
    ):
        self.receipt = receipt
        self.chain_id = chain_id
        self.error = error


async def _eth_chain_id(ctx: ExecutionContext, *, connected: ConnectedAccountRef | None) -> _ChainIdResult:
    try:
        result = await _rpc(ctx, method="eth_chainId", params=[], connected=connected)
    except _RpcMisconfiguration as exc:
        return _ChainIdResult(value=None, error=_rpc_misconfiguration(ctx, exc, method="eth_chainId"))
    except _RpcTimeout as exc:
        return _ChainIdResult(value=None, error=_rpc_timeout(ctx, exc))
    except _RpcError as exc:
        return _ChainIdResult(value=None, error=_rpc_failure(ctx, exc))
    try:
        if isinstance(result, str) and result.startswith("0x"):
            return _ChainIdResult(value=int(result, 16))
        return _ChainIdResult(value=int(result))
    except Exception:
        return _ChainIdResult(
            value=None,
            error=_structured_error(
                ctx,
                error_code="rpc_invalid_response",
                message="eth_chainId returned an unparseable result.",
                details={"result": result},
            ),
        )


async def _eth_get_balance(
    ctx: ExecutionContext,
    address: str,
    *,
    connected: ConnectedAccountRef | None,
) -> _BalanceResult:
    chain_id = await _eth_chain_id(ctx, connected=connected)
    if chain_id.error is not None:
        return _BalanceResult(wei=0, eth="0", chain_id=None, error=chain_id.error)
    try:
        result = await _rpc(ctx, method="eth_getBalance", params=[address, "latest"], connected=connected)
    except _RpcMisconfiguration as exc:
        return _BalanceResult(wei=0, eth="0", chain_id=chain_id.value, error=_rpc_misconfiguration(ctx, exc, method="eth_getBalance"))
    except _RpcTimeout as exc:
        return _BalanceResult(wei=0, eth="0", chain_id=chain_id.value, error=_rpc_timeout(ctx, exc))
    except _RpcError as exc:
        return _BalanceResult(wei=0, eth="0", chain_id=chain_id.value, error=_rpc_failure(ctx, exc))
    try:
        if isinstance(result, str) and result.startswith("0x"):
            wei = int(result, 16)
        else:
            wei = int(result)
    except Exception:
        return _BalanceResult(
            wei=0,
            eth="0",
            chain_id=chain_id.value,
            error=_structured_error(
                ctx,
                error_code="rpc_invalid_response",
                message="eth_getBalance returned an unparseable result.",
                details={"result": result},
            ),
        )
    eth = _format_eth_from_wei(wei)
    return _BalanceResult(wei=wei, eth=eth, chain_id=chain_id.value)


async def _eth_get_transaction_receipt(
    ctx: ExecutionContext,
    tx_hash: str,
    *,
    connected: ConnectedAccountRef | None,
) -> _ReceiptResult:
    chain_id = await _eth_chain_id(ctx, connected=connected)
    if chain_id.error is not None:
        return _ReceiptResult(receipt=None, chain_id=None, error=chain_id.error)
    try:
        result = await _rpc(ctx, method="eth_getTransactionReceipt", params=[tx_hash], connected=connected)
    except _RpcMisconfiguration as exc:
        return _ReceiptResult(receipt=None, chain_id=chain_id.value, error=_rpc_misconfiguration(ctx, exc, method="eth_getTransactionReceipt"))
    except _RpcTimeout as exc:
        return _ReceiptResult(receipt=None, chain_id=chain_id.value, error=_rpc_timeout(ctx, exc))
    except _RpcError as exc:
        return _ReceiptResult(receipt=None, chain_id=chain_id.value, error=_rpc_failure(ctx, exc))
    if result is None:
        return _ReceiptResult(receipt={}, chain_id=chain_id.value)
    if not isinstance(result, dict):
        return _ReceiptResult(
            receipt=None,
            chain_id=chain_id.value,
            error=_structured_error(
                ctx,
                error_code="rpc_invalid_response",
                message="eth_getTransactionReceipt returned a non-object result.",
                details={"result": result},
            ),
        )
    return _ReceiptResult(receipt=dict(result), chain_id=chain_id.value)


def _format_eth_from_wei(wei: int) -> str:
    if wei < 0:
        return "0"
    whole = wei // 10**18
    frac = wei % 10**18
    if frac == 0:
        return str(whole)
    frac_text = f"{frac:018d}".rstrip("0")
    return f"{whole}.{frac_text}"


class _RpcError(RuntimeError):
    def __init__(self, *, method: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.method = method
        self.details = details


class _RpcTimeout(RuntimeError):
    def __init__(self, *, method: str, timeout_seconds: float):
        super().__init__(f"RPC timeout after {timeout_seconds}s")
        self.method = method
        self.timeout_seconds = timeout_seconds


class _RpcMisconfiguration(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)


def _effective_environment_value(ctx: ExecutionContext) -> str:
    ctx_env = getattr(ctx, "environment", None)
    if ctx_env is not None:
        return str(getattr(ctx_env, "value", ctx_env) or "").strip().lower()
    return str(os.environ.get("SIGLUME_ENV") or "").strip().lower()


def _is_live_environment(ctx: ExecutionContext) -> bool:
    value = _effective_environment_value(ctx)
    if value in {"sandbox", "test", "testing", "dev", "development"}:
        return False
    if value in {"live", "prod", "production"}:
        return True
    # Unknown -> conservative: treat as live (no stub fallback).
    return True


async def _rpc(ctx: ExecutionContext, *, method: str, params: list[Any], connected: ConnectedAccountRef | None) -> Any:
    rpc_url = _rpc_url_from_env()
    if not rpc_url:
        if _is_live_environment(ctx):
            raise _RpcMisconfiguration(
                "METAMASK_RPC_URL is not set. Configure a public Ethereum JSON-RPC endpoint for live execution."
            )
        return _stub_rpc(method=method, params=params)

    if not _has_connected_metamask(connected):
        return _stub_rpc(method=method, params=params)

    timeout = 8.0
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        raise _RpcTimeout(method=method, timeout_seconds=timeout)
    except Exception as exc:
        raise _RpcError(method=method, message="RPC request failed.", details=str(exc))

    if not isinstance(data, dict):
        raise _RpcError(method=method, message="RPC response is not a JSON object.", details=data)
    if "error" in data:
        raise _RpcError(method=method, message="RPC returned an error.", details=data.get("error"))
    return data.get("result")


def _rpc_timeout(ctx: ExecutionContext, exc: _RpcTimeout) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        execution_kind=ctx.execution_kind,
        error_message=str(exc),
        provider_status="timeout",
        output={"summary": "RPC timeout.", "amount_usd": 0.0, "currency": "USD"},
        receipt_summary={
            "type": "error_receipt",
            "error_code": "rpc_timeout",
            "message": "RPC call timed out.",
            "details": {"method": exc.method, "timeout_seconds": exc.timeout_seconds},
            "phase": "phase1",
            "provider": "metamask",
        },
        needs_approval=False,
        units_consumed=1,
    )


def _rpc_failure(ctx: ExecutionContext, exc: _RpcError) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        execution_kind=ctx.execution_kind,
        error_message=str(exc),
        provider_status="error",
        output={"summary": "RPC failure.", "amount_usd": 0.0, "currency": "USD"},
        receipt_summary={
            "type": "error_receipt",
            "error_code": "rpc_error",
            "message": "RPC call failed.",
            "details": {"method": exc.method, "error": str(exc), "rpc_details": exc.details},
            "phase": "phase1",
            "provider": "metamask",
        },
        needs_approval=False,
        units_consumed=1,
    )


def _rpc_misconfiguration(ctx: ExecutionContext, exc: _RpcMisconfiguration, *, method: str) -> ExecutionResult:
    message = str(exc)
    return ExecutionResult(
        success=False,
        execution_kind=ctx.execution_kind,
        error_message=message,
        provider_status="misconfigured",
        output={"summary": message, "amount_usd": 0.0, "currency": "USD"},
        receipt_summary={
            "type": "error_receipt",
            "error_code": "misconfiguration",
            "message": message,
            "details": {"method": method, "environment": _effective_environment_value(ctx)},
            "phase": "phase1",
            "provider": "metamask",
        },
        needs_approval=False,
        units_consumed=1,
    )


def _stub_rpc(*, method: str, params: list[Any]) -> Any:
    if method == "eth_chainId":
        return "0x1"
    if method == "eth_getBalance":
        return hex(1_234_500_000_000_000_000)
    if method == "eth_getTransactionReceipt":
        tx_hash = str(params[0]) if params else "0x" + ("0" * 64)
        return {
            "transactionHash": tx_hash,
            "blockNumber": hex(12_345_678),
            "status": "0x1",
            "gasUsed": hex(21_000)
        }
    raise _RpcError(method=method, message="RPC method not supported in stub.", details={"params": params})


# ---- EIP-55 checksum (Keccak-256) ----

_MASK64 = (1 << 64) - 1

_ROTATION_OFFSETS = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_ROUND_CONSTANTS = [
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008
]


def _rotl64(x: int, n: int) -> int:
    n &= 63
    return ((x << n) & _MASK64) | ((x & _MASK64) >> (64 - n))


def _keccak_f1600(state: list[int]) -> None:
    for rc in _ROUND_CONSTANTS:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]

        b = [0] * 25
        for x in range(5):
            for y in range(5):
                idx = x + 5 * y
                rot = _ROTATION_OFFSETS[x][y]
                nx = y
                ny = (2 * x + 3 * y) % 5
                b[nx + 5 * ny] = _rotl64(state[idx], rot)

        for x in range(5):
            for y in range(5):
                idx = x + 5 * y
                state[idx] = b[idx] ^ ((~b[((x + 1) % 5) + 5 * y] & _MASK64) & b[((x + 2) % 5) + 5 * y])

        state[0] ^= rc


def _keccak_256(data: bytes) -> bytes:
    rate = 136
    state = [0] * 25

    offset = 0
    while offset + rate <= len(data):
        block = data[offset : offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8 : (i + 1) * 8], "little")
            state[i] ^= lane
        _keccak_f1600(state)
        offset += rate

    tail = bytearray(data[offset:])
    tail.append(0x01)
    while len(tail) < rate:
        tail.append(0x00)
    tail[-1] |= 0x80

    for i in range(rate // 8):
        lane = int.from_bytes(tail[i * 8 : (i + 1) * 8], "little")
        state[i] ^= lane
    _keccak_f1600(state)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            out.extend(state[i].to_bytes(8, "little"))
            if len(out) >= 32:
                break
        if len(out) < 32:
            _keccak_f1600(state)
    return bytes(out[:32])


def _to_checksum_address(address: str) -> str:
    addr = address[2:] if address.startswith(("0x", "0X")) else address
    addr_lower = addr.lower()
    digest = _keccak_256(addr_lower.encode("ascii")).hex()
    out = ["0x"]
    for i, ch in enumerate(addr_lower):
        if ch in "0123456789":
            out.append(ch)
        else:
            out.append(ch.upper() if int(digest[i], 16) >= 8 else ch)
    return "".join(out)
