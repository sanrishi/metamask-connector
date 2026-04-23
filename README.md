## MetaMask Connector (Phase 1)

High-safety wallet API template for Siglume agents.

### Implemented (Phase 1)
- Read-only Ethereum JSON-RPC:
  - `eth_chainId`
  - `eth_getBalance`
  - `eth_getTransactionReceipt`
- Strict EIP-55 checksum validation for address inputs
- Structured error receipts for: RPC timeout, network mismatch, invalid address

### Stubbed (Phase 2/3)
`ExecutionKind.QUOTE` and `ExecutionKind.PAYMENT` are intentionally stubbed:
- `success=True`
- `needs_approval=False`
- `side_effects=[]`
- `receipt_summary.note = "Phase 2/3 not yet implemented  stub only"`

This keeps the harness happy without faking real payment behavior.

### Local test
```bash
siglume test .
```

### Config (optional)
Set `METAMASK_RPC_URL` to call a real Ethereum JSON-RPC endpoint. If not set,
the adapter uses a deterministic in-process stub so local harness checks pass.

