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
                     MCP stdio server (read-only query surface) ◄─┘
                     search_events / get_session / timeline /
                     find_sessions / activity_stats / add_insight*
                     (*the one write tool: distilled conclusions only,
                      executed in a separate sanitized child process)
```

- **Progressive disclosure** — `search_events` returns cheap session cards by default
  (`detail=index`), expand to snippets (`context`) or full lines (`full`) on demand, so
  recall doesn't blow your context window. Queries are **multi-term AND** (space-separated;
  quote a phrase to keep it literal), ranked **per session by BM25** rather than recency,
  with a per-session quota so one huge session can't crowd out the rest. Transcripts
  younger than 60 seconds are not read (half-written JSONL), and the server knows which
  agent launched it (`--self-agent`, written by `connect`), so a query never ranks the
  conversation that is asking.
- **Insights layer** — beyond raw transcripts, agents can deposit distilled
  `decision / fact / artifact / lesson` rows with a pointer back to the source session,
  so “what did we decide?” is a first-class answer. Re-depositing the same
  project+type+text refreshes its timestamp instead of duplicating it, and results show
  the age plus the referenced files so stale conclusions are visibly stale.
- **Lazy freshness** — every query triggers a throttled (~45s) incremental re-sync in a
  child process, so a conversation that just ended is already searchable. Writes are
  **append-only** (per-session fingerprint diff, full replay only when old events
  disappear) and the audit is incremental too (new rows only, full scan weekly), which
  keeps a sync at a few hundred milliseconds instead of rebuilding the whole FTS.
  The server itself stays physically read-only (`PRAGMA query_only`).

## Tools (MCP)

| tool | what |
|---|---|
| `search_events` | full-text (CJK + substring) search over all agents' history, with filters |
| `get_session` | expand one session's event stream (paged, role filter) |
| `timeline` | sessions around an anchor — “what else was I doing then?” |
| `find_sessions` | list sessions by project/agent/model/date (project aliases expand) |
| `activity_stats` | recent activity rollup |
| `add_insight` | deposit a distilled decision/fact/artifact/lesson (sanitized, deduped, pointer-validated; runs in a child process — the server itself stays read-only) |
| `add_insight` | deposit a distilled decision/fact/artifact/lesson (deduped, sanitized, delegated to a child process) |

## Safety model

- **Read-only query surface.** The MCP server opens the DB `mode=ro` + `query_only`; a
  write attempt errors out. The only write tool, `add_insight`, delegates to
  `insight_add.py` in a child process (same sanitize + audit rules), and sync also runs
  out-of-process — the server itself never opens a writable connection.
- **Startup gate.** If `audit_secrets.py` hasn't passed recently, the server exits 2
  instead of serving. Unaudited data never leaves through this door.
- **Bounded output.** Snippets are hard-capped; there is no `SELECT *` escape hatch.
- **Redaction at write time.** API keys / bearer tokens / AK-secret pairs / data-URLs
  are replaced before rows commit; the daily audit re-scans the whole DB and fails loud.
- **Local only.** No network, no telemetry, no cloud.

## Repo layout

```
agentmem.py          unified CLI (detect/init/sync/status/search/serve/connect)
eval_ab.py           A/B harness: git-HEAD querylib vs working tree, on the same questions
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

## Benchmark

`recall_eval.py` generates a self-test straight from **your own corpus**: it extracts
distinctive phrases from session text (unique-tier = phrase lives in exactly one session;
ambiguous-tier = 2–3 sessions) and measures where the target session ranks in the same
`detail=index` result path the MCP server uses. Fully reproducible on your machine with
one command; questions are derived from private data, so only aggregate numbers are
for publication — the question dump is gitignored.

Results on the author's corpus (178 sessions / 33,458 events across 6 agents, 142 auto-generated questions):

| tier | hit@1 | hit@3 | recall@20 | MRR |
|---|---|---|---|---|
| unique (find the one session) | **100%** | 100% | 100% | 1.00 |
| ambiguous (right one on top) | 64.3% | **100%** | 100% | 0.81 |
| overall | 82.4% | **100%** | 100% | 0.91 |

Query latency: median **0.2 ms**, p95 1.7 ms.

```bash
python recall_eval.py --run --n 20        # your numbers on your corpus
```

`recall_eval` only asks phrase-shaped questions (its 100% is a phrase-lookup score). The
retrieval rewrite was justified with `eval_ab.py`, which replays the **same questions**
through `git HEAD`'s querylib and the working tree (111 questions, current corpus):

| tier | hit@1 old → new | hit@3 old → new | recall@20 old → new |
|---|---|---|---|
| phrase (旧路径甜区) | 51.4% → 64.9% | 64.9% → 75.7% | 94.6% → **100%** |
| multi-term（空格 AND） | 0% → **78.4%** | 0% → **89.2%** | 0% → **94.6%** |
| 2-char（LIKE 兜底） | 2.7% → 8.1% | 16.2% → 27.0% | 64.9% → 54.1% ⚠ |

Honest note: multi-term queries went from unusable (0%) to working, which was the point of
the rewrite. The 2-char tier's recall@20 dipped (⚠): per-session quota + relevance-first
ranking can crowd out rare short-word hits that recency-ordering happened to surface.
Open for a PR; the phrase/multi gains are net-positive regardless.
```bash
python eval_ab.py --n 40                  # rebuild the old-code baseline on your corpus
```

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
