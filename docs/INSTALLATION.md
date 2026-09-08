# Installing dspace-mcp into AI-agent systems

dspace-mcp is a standard MCP server over **stdio**. Once it is installed and
configured, any MCP-capable AI client can launch it the same way: run
`dspace-mcp` (via `uv`) as a child process and talk JSON-RPC over stdin/stdout.

This guide covers the shared setup steps once, then shows the exact
registration snippet for each supported client.

---

## 1. Prerequisites

- **Python >= 3.10** and [`uv`](https://docs.astral.sh/uv/) on the machine that
  will host the server. (If you prefer, any MCP client can instead run the
  entrypoint with a plain Python virtualenv; `uv` is only the documented
  shortcut.)
- **SSH access to the DSpace host** for the server-side tools. The local tools
  (SAF building, OJS conversion, scientometric harvesting, OA download, LLM
  enrichment) need no DSpace host at all.
- A configuration file (see below).

> The agent host and the DSpace host are usually different machines. dspace-mcp
> runs **on the agent host** and reaches the DSpace installation over SSH. Put
> the SSH key on the agent host, and grant NOPASSWD sudo for the specific
> `dspace` CLI/chown commands (see `README.md` "Configure").

## 2. Install the server

```bash
git clone https://github.com/or-sirina/dspace-mcp.git
cd dspace-mcp
uv venv .venv
uv pip install -e . --python .venv/bin/python
```

Verify the entrypoint is present:

```bash
uv run dspace-mcp --help   # starts the stdio server; exit with Ctrl-C / EOF
```

## 3. Configure

```bash
mkdir -p ~/.config/dspace-mcp
cp config.example.toml ~/.config/dspace-mcp/config.toml
```

Fill in one `[profile.<name>]` block per DSpace instance. **Never put secrets
in the file** — any string of the form `"env:VAR_NAME"` is read from the
process environment at launch time:

```toml
[profile.mgimo.db]
password = "env:DSPACE_DB_PASSWORD"
```

To use a config file elsewhere (e.g. a project checkout), set the
`DSPACE_MCP_CONFIG` environment variable in the client's `env` block (shown
below). If only one profile is configured it is selected automatically;
otherwise set `DSPACE_MCP_PROFILE` or pass `profile` on each tool call.

## 4. The canonical command

Every snippet below is the same command wrapped in that client's config syntax:

```
uv --directory /absolute/path/to/dspace-mcp run dspace-mcp
```

Use an **absolute** path — MCP clients spawn the child from their own working
directory and do not resolve `~`.

## 5. Smoke-test the connection

After registering, ask the agent to call `dspace_ping` (read-only). It returns
SSH reachability, `dspace version`, Postgres, Tomcat, and Solr status. If you
have no live DSpace host yet, call a local tool such as `saf_generate_manifest`
or `extract_pdf_text` to confirm the stdio transport itself works.

---

## Claude Desktop

Config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

(Claude Desktop > Settings > Developer > Edit Config.)

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": {
        "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml",
        "DSPACE_DB_PASSWORD": "...",
        "DEEPSEEK_API_KEY": "..."
      }
    }
  }
}
```

Restart Claude Desktop. The server appears under the tools (🔌) menu.

## Claude Code (CLI)

One-line registration (user scope):

```bash
claude mcp add dspace-mcp \
  --env DSPACE_MCP_CONFIG=/absolute/path/to/config.toml \
  -- uv --directory /absolute/path/to/dspace-mcp run dspace-mcp
```

For a **project-scoped** server shared with the team, add `.mcp.json` at the
repository root:

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": { "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml" }
    }
  }
}
```

Verify with `claude mcp list` and `/mcp`.

## Cursor

Project-scoped: create `.cursor/mcp.json` at the workspace root.

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": { "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml" }
    }
  }
}
```

Global: Cursor Settings → MCP → Add new MCP server, then paste the same
`command`/`args`/`env` fields. Reload the window after adding.

## VS Code

### Copilot (agent mode)

Create `.vscode/mcp.json` (note the top-level key is `servers`, not
`mcpServers`):

```json
{
  "servers": {
    "dspace-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": { "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml" }
    }
  }
}
```

Enable in the Copilot Chat agent-mode toolbar (Tools → MCP). Requires a recent
VS Code with Copilot agent mode.

### Cline / Roo Code

Both read the same `mcpServers` shape. In the MCP Servers view, add a new
server, or edit the JSON directly:

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": { "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml" },
      "autoApprove": []
    }
  }
}
```

A project-level `.mcp.json` at the workspace root is also picked up by both
extensions.

## Continue.dev

Edit `~/.continue/config.yaml` (or the project `.continue/config.yaml`):

```yaml
mcpServers:
  - name: dspace-mcp
    command: uv
    args:
      - --directory
      - /absolute/path/to/dspace-mcp
      - run
      - dspace-mcp
    env:
      DSPACE_MCP_CONFIG: /absolute/path/to/config.toml
```

Reload the Continue extension; the server's tools become available to the
configured models.

## opencode

Project config `opencode.json` (or global `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dspace-mcp": {
      "type": "local",
      "command": ["uv", "--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "environment": {
        "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml"
      },
      "enabled": true
    }
  }
}
```

List servers with `/mcp` in the opencode TUI.

## DeepSeek Harness (DSH)

DSH connects MCP servers through its `@deepseek-ai/dsh-mcp-client` plugin. Add
a Cordis overlay (YAML) and pass it with `--patch`, or merge it into the user
patch layer `$DSH_HOME/cordis.patch.yml`.

`dspace-mcp.cordis.yml`:

```yaml
- insert:
    - id: mcp-dspace
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dspace-mcp
        transport: stdio
        command: uv
        args:
          - --directory
          - /absolute/path/to/dspace-mcp
          - run
          - dspace-mcp
        env:
          DSPACE_MCP_CONFIG: /absolute/path/to/config.toml
        cwd: /absolute/path/to/dspace-mcp
        toolCallTimeoutMs: 300000
        failOnStartupError: true
```

Enable it:

```sh
dsh web --patch "$PWD/dspace-mcp.cordis.yml"
```

Tools are exposed to the model as `mcp__dspace-mcp__<tool>`. Note that DSH's
stdio bridge strips ambient variables whose names look like credentials and all
`DSH_*` variables; put every secret your profile needs in the `config.env`
block above.

## Generic stdio client

Any MCP client that accepts a JSON `mcpServers` map works with the canonical
form:

```json
{
  "mcpServers": {
    "dspace-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dspace-mcp", "run", "dspace-mcp"],
      "env": {
        "DSPACE_MCP_CONFIG": "/absolute/path/to/config.toml",
        "DSPACE_MCP_PROFILE": "mgimo"
      }
    }
  }
}
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `No config file at ...` | Create `~/.config/dspace-mcp/config.toml` (copy the example) or set `DSPACE_MCP_CONFIG`. |
| `config references env:VAR ... not set` | Set that environment variable in the client's `env` block or your shell. |
| `Multiple profiles configured` | Set `DSPACE_MCP_PROFILE` or pass `profile=` on tool calls. |
| Tool returns `dry_run` / `executed: false` | Expected: state-changing tools preview by default. Pass `confirm=True` to run. |
| `blocked ... allow_db_write=True` | Expected: direct DB/assetstore writes need the extra flag (and auto-back up first). |
| `dspace_ping` fails `ssh_ok: false` | Check SSH key/agent, host reachability, and `ConnectTimeout`. |
| Password visible in `ps` | Grant NOPASSWD sudo; see `README.md` security notes. |

For anything else, run `dspace_ping` first and read the returned diagnostic
fields — they pinpoint SSH/DB/Tomcat/Solr failures independently.
