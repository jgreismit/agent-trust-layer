# L4 notes

## Plan

1. atl/service.py: ATLService with injected store, rail factory, receipt chain, evaluate_fn, keys, runlog, clock and input; a reentrant thread plus fcntl file lock (var/locks/execute.lock) serializes every status change across processes.
2. atl/approvals/: signed ApprovalRecord helpers (sign and verify), production guards (caps, allowlist, PAY prompt with masked key) and production.py for `atl demo --production-single` (dry run on the sandbox rail when ATL_ENV=sandbox).
3. atl/mcp_server.py: MCPServer("atl") (mcp 2.x name of FastMCP, with a 1.x fallback import), six typed tools, lazy service creation so the server starts even on stubs, one tool_call runlog line per call, nothing on stdout.
4. atl/cli.py: typer commands approve, refuse, revoke, rail balance, reset, mandate register, demo; every command calls load_settings first and maps ATLError to "CODE: message" plus exit 2.
5. Tests with in-memory fakes only (store, chain, evaluate, rail, verify): flows, S-40, S-41, S-42, S-45, S-46, bad mandate signature, approval signature, tool call log lines, in-process MCP client.

## Findings and interpretations

- Installed mcp is 2.3.0: `mcp.server.fastmcp` no longer exists; FastMCP is `mcp.server.mcpserver.MCPServer`. The server imports MCPServer and falls back to FastMCP on mcp 1.x.
- execute_payment also refuses (DECISION_NOT_APPROVED) when the mandate was revoked after approval. Spec only says later pay_check calls are refused; blocking execution is the safer reading.
- Production daily limit (1000 cents) sums transfers in receipts on the same rail since midnight America/Sao_Paulo.
- `--production-single` in sandbox enables the production guards (caps, allowlist, PAY prompt) on the service with the allowlisted key `cliente-a00004@pix.bcb.gov.br`, so the dry run exercises the same code path as production.
- The MCP server passes an input_fn that always returns "" so a production payment can never be confirmed through the agent channel.
- Approval signatures use the JSON (pydantic) serialization of ApprovalRecord.created_at as `created_at_iso`; `atl.approvals.verify_approval` recomputes it the same way.
