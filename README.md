## MetaMask Connector (Read-only)

Read-only Ethereum JSON-RPC lookups for Siglume agents.

### Implemented
- Read-only Ethereum JSON-RPC:
  - `eth_chainId`
  - `eth_getBalance`
  - `eth_getTransactionReceipt`
- Strict EIP-55 checksum validation for address inputs
- Structured error receipts for: RPC timeout, network mismatch, invalid address

### Local test
```bash
siglume test .
```

### Config (optional)
Set `METAMASK_RPC_URL` to call a real Ethereum JSON-RPC endpoint. If not set,
the adapter uses a deterministic in-process stub so local harness checks pass.
