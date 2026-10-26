# agentmem

**Machine-wide shared memory for your AI agents.** One read-only MCP server that
indexes the session history of every coding/chat agent on your machine and lets any
of them search what the *others* already did — decisions, fixes, configs, command
flags, lessons — retroactively, locally, with secrets redacted at write time.

Your agents already write their conversations to disk. agentmem reads all of it,
normalizes six very different private storage formats into one event index, strips
API keys before a single row is committed, and hands the right memory back to
whichever agent you're talking to.

> deja-vu did this for the Western CLI-agent ecosystem (Claude Code / Codex / Cursor…).
> agentmem is the same idea **for the Chinese desktop-agent ecosystem**
> (WorkBuddy / DeepSeek Harness / Kilo / OpenCode), plus two things nobody else ships:
> **billing-grade token reconciliation** (verified to ±0% against provider invoices) and
> an **audit gate** (if the redaction audit fails, the server refuses to serve).

---

## Why

Multi-agent workflows leak memory. You fix a gnarly Unity NavMesh limit in one tool,
months later another tool re-solves it from scratch. You configure a relay endpoint in
Codex, then Claude Code has to be told again. Every agent forgets what the others learned.

agentmem gives the whole fleet **one searchable memory**, without asking any agent to
change how it works:

- **Retroactive & zero-injection** — no hooks in the agent's hot loop, no per-turn
  capture calls. It parses existing session files. Install it today and last month's
  history is searchable on first query.
- **Deep parsing, not note-taking** — real event streams (user / assistant / tool_call),
  so an agent can recover *exactly which command fixed it*, not a summary of a summary.
- **Privacy-first, enforced** — secrets redacted at write time; a daily full-DB audit
  asserts zero residue; the MCP server won't start if the audit hasn't passed. Data
  never leaves your machine.

## Supported agents

| Agent | Data source (index) | MCP client (recall) |
|---|---|---|
| WorkBuddy | ✅ (JSONL transcripts + credit/token) | ✅ |
| Codex | ✅ (rollout JSONL) | ✅ (config.toml) |
| Claude Code | ✅ (`~/.claude/projects` JSONL) | ✅ (`.claude.json`) |
| Kilo | ✅ (SQLite `kilo.db`) | ✅ (kilo.json) |
| OpenCode | ✅ (SQLite `opencode.db`) | ✅ (opencode.json) |
| DeepSeek Harness | ✅ (zstd event stream) | ✅ (dsh-mcp-client patch) |
| Claude Desktop | ❌ no local transcript (cloud) | ✅ config-based |
| anything else | drop in a `parsers/<app>.py` (see below) | ✅ |

Adding a new source is one file: copy `parsers/_template.py`, map its records to
`SessionBundle`, done — pipeline discovery, redaction, audit, FTS and the MCP server
all pick it up automatically, no core edits.

## Quick start

```bash
pip install -r requirements.txt          # only zstandard (optional, for DeepSeek Harness)
python agentmem.py detect                # what agents/clients are on this machine?
python agentmem.py init --agents workbuddy,codex,claude,kilo,opencode,dsh
python agentmem.py connect --all         # diff shown first; --yes writes after backup
# restart your agents → they can now search each other's history via the `agentmem` tools
```

`connect` follows a strict policy: it **prints the exact entry it will add, backs up the
config, and only merges its own key** — it never touches your other MCP servers.

## How it works

```
 6 agents' private stores ──► parsers/*.py ──► sanitize ──► agentmem.db
   (JSONL / SQLite / zstd)      (pluggable)     (keys out)   (sessions/events/usage
                                                               + trigram FTS index)
                                                                  │
                     MCP stdio server (read-only, gated) ◄───────┘
                     search_events / get_session / timeline /
                     find_sessions / activity_stats
```

- **Progressive disclosure** — `search_events` returns cheap session cards by default
  (`detail=index`), expand to snippets (`context`) or full lines (`full`) on demand, so
  recall doesn't blow your context window.
- **Insights layer** — beyond raw transcripts, agents can deposit distilled
  `decision / fact / artifact / lesson` rows with a pointer back to the source session,
  so “what did we decide?” is a first-class answer.
- **Lazy freshness** — every query triggers a throttled (~45s) incremental re-sync in a
  child process, so a conversation that just ended is already searchable. The server
  itself stays physically read-only (`PRAGMA query_only`).

## Tools (MCP)

| tool | what |
|---|---|
| `search_events` | full-text (CJK + substring) search over all agents' history, with filters |
| `get_session` | expand one session's event stream (paged, role filter) |
| `timeline` | sessions around an anchor — “what else was I doing then?” |
| `find_sessions` | list sessions by project/agent/model/date (project aliases expand) |
| `activity_stats` | recent activity rollup |

## Safety model

- **Physical read-only.** The MCP server opens the DB `mode=ro` + `query_only`; a write
  attempt errors out. Sync happens in separate child processes.
- **Startup gate.** If `audit_secrets.py` hasn't passed recently, the server exits 2
  instead of serving. Unaudited data never leaves through this door.
- **Bounded output.** Snippets are hard-capped; there is no `SELECT *` escape hatch.
- **Redaction at write time.** API keys / bearer tokens / AK-secret pairs / data-URLs
  are replaced before rows commit; the daily audit re-scans the whole DB and fails loud.
- **Local only.** No network, no telemetry, no cloud.

## Repo layout

```
agentmem.py          unified CLI (detect/init/sync/status/search/serve/connect)
core.py pipeline.py  session-level idempotent writer + orchestrator (plugin discovery)
parsers/*.py         one per agent source (_template.py = copy to add one)
sanitize.py audit_secrets.py   write-time redaction + full-DB gate
agentmem_db.py querylib.py     schema (+FTS/trigram/insights/projects) + shared search
mcp_agentmem.py      the MCP stdio server (read-only)
connect.py prescan.py          opt-in client wiring (diff+backup+merge) + release privacy gate
dashboard.py hook_session_start.py  static dashboard + auto-inject recall
```

`agentmem.db`, `dashboard.html`, backups and `agentmem.config.json` are gitignored —
**your actual history never enters the repo.**

## Limitations / honest notes

- Windows-first (paths + a couple of Chinese-Windows stdio quirks handled). macOS/Linux
  detection is interface-only for now.
- DeepSeek Harness needs `zstandard`; absent → that source is skipped, others fine.
- Claude Desktop & cloud chat apps (e.g. Doubao) have **no local transcript**: they can
  be a recall client, not an indexed source.
- This is a personal-scale tool (tens of thousands of events → millisecond queries);
  for gigabyte histories you'd want the vector-index path left as a documented next step.

## License

MIT.
