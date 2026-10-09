# OpenClaw-owned remote MCP provisioning

`OpenClawSpec.mcp_servers` declares remote HTTPS OAuth servers. Clawake renders
native OpenClaw configuration, never connects to MCP, handles OAuth tokens or
acts as a CAD client. OAuth login/logout, connection caching and disposal remain
OpenClaw responsibilities. Run authentication in the selected instance, not in
the operator's coding-agent MCP configuration.

Example inventory fragment:

```yaml
openclaw:
  lead_agent_id: engineer
  mcp_servers:
    cad:
      url: https://mcp.example.test/mcp
      enabled: false
      tools: [inspect_revision, apply_change]
      oauth_scope: read write
      request_timeout_ms: 30000
      connection_timeout_ms: 10000
```

Servers default to disabled, require exact unique provider tool names and explicit
OAuth scopes, verify TLS and disable parallel tool calls. Embedded credentials,
query/fragment parameters, insecure HTTP and wildcard tool filters are rejected.
No local/stdio transport or provider-specific implementation is added here.

MCP provisioning **does not add agent grants**. Independently review the existing
`agent_tool_allow` inventory using actual OpenClaw names such as
`cad__inspect_revision`. Deploy to the capability owner's instance only; do not
give other agents access through wildcard, plugin-wide or default tool grants.
A workflow gate must still guard side effects; a tool grant/OAuth consent is not
human approval. Native Codex projection is not enabled/configured by this change.

The existing runtime ownership ledger manages each declared server as one object.
Plans are read-only and contain no token expansion. Unmanaged entries survive.
Conflicting unmanaged or externally modified managed objects cause an error.
Removal retires only declared managed entries; OpenClaw owns OAuth credential
retention/logout. Existing per-file atomicity limitations remain; this is not a
crash-atomic multi-file transaction. Normal Clawake deployment/lifecycle remains
the only runtime manager.

See [OpenClaw configuration](https://docs.openclaw.ai/gateway/config-extensions)
and [native MCP CLI](https://docs.openclaw.ai/cli/mcp). The contract was checked
against OpenClaw 2026.9.4; it is not a compatibility promise for all releases.

Tests cover unsafe configuration, disabled defaults, no implicit grants,
preservation of unrelated servers/tools, no-op planning, removal and drift.
