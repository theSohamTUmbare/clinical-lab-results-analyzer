# Using the knowledge server from another MCP client

The clinical knowledge server is a standard MCP server over stdio, so the FastAPI
agent is not the only thing that can drive it. Pointing any MCP client at it gives
that client the same reference intervals, alias resolution and severity rules the
app uses — which is a large part of why the knowledge lives behind MCP rather than
inside the API layer.

## Claude Desktop

Add this to `claude_desktop_config.json`, replacing the paths with your own:

```json
{
  "mcpServers": {
    "clinical-lab-knowledge": {
      "command": "C:/path/to/Aragen-hackathon/backend/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:/path/to/Aragen-hackathon/backend",
      "env": { "PYTHONPATH": "C:/path/to/Aragen-hackathon/backend" }
    }
  }
}
```

On macOS or Linux use `.venv/bin/python` instead.

## Claude Code

```bash
claude mcp add clinical-lab-knowledge \
  --command ./.venv/bin/python \
  --args "-m,mcp_server.server" \
  --cwd ./backend
```

## Running it directly

```bash
cd backend
.venv/Scripts/python -m mcp_server.server
```

It then speaks MCP on stdin/stdout and waits for a client.

## Tools exposed

| Tool | Purpose |
|---|---|
| `classify_lab_result` | Classify one result; returns status plus the full evidence trail. |
| `reference_range_lookup` | Sex/age-appropriate interval with provenance, plus candidates when a name does not resolve. |
| `resolve_test_name` | Map a free-text name onto a catalogue concept. |
| `care_pathway_lookup` | Protocol follow-up actions for a (test, status, direction). |
| `parse_reference_range` | Parse `"15-150"`, `"<5"`, `">=90"` into numeric bounds. |
| `list_catalog` | Every supported test with unit, category and LOINC code. |

Every tool returns JSON as text and never invents a value it does not hold: an
unresolvable name comes back as `matched_via: "unresolved"` with candidates, not
as a guess.
