# re-agent

A reverse engineering agent that gives Claude a **live, queryable model of a binary** instead of screenshots and vibes.

The agent connects to a running [Binary Ninja](https://binary.ninja) session through a local HTTP bridge, then uses Claude's tool-calling API to navigate the binary systematically — inspecting functions, tracing call graphs, reading decompiled code, and writing structured notes — until it can answer a question about the binary with evidence.

---

## Why this exists

Most "AI-assisted RE" demos paste decompiled text into a chat window and ask Claude to guess what it does. That works for toy examples. It breaks on real binaries because:

- Context fills up fast when you paste whole functions
- The agent has no memory between turns
- There's no way to pivot — no xrefs, no callers, no string search
- Pseudocode from HLIL can lie; you need to drop to MLIL or LLIL to verify

This project wires Claude directly into Binary Ninja's analysis engine so the agent can navigate the binary the same way a human analyst would: start from imports and strings, rank suspicious functions, decompile one function at a time, follow call edges, form hypotheses, and verify them with cross-references.

---

## How it works

```
┌─────────────────────────────────────────┐
│           Binary Ninja (GUI)            │
│                                         │
│  bn_bridge/plugin.py                    │
│  └─ HTTP server on localhost:7734       │
│     exposes: session, overview,         │
│     list_functions, decompile,          │
│     callers, xrefs, rename, patch...    │
└────────────────┬────────────────────────┘
                 │ HTTP (JSON)
                 ▼
┌─────────────────────────────────────────┐
│           agent/client.py               │
│                                         │
│  REAgent                                │
│  ├─ 17 Claude tools → bridge calls     │
│  ├─ tool-calling loop (up to 40 turns) │
│  └─ Notebook injected into system      │
│     prompt each turn (no amnesia)      │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│        agent/loops/triage.py           │
│                                         │
│  5-phase malware triage:               │
│  1. Entry scan (imports + strings)     │
│  2. Function clustering                │
│  3. Main path reconstruction           │
│  4. Hypothesis verification            │
│  5. Structured conclusion report       │
└─────────────────────────────────────────┘
```

The agent maintains a **sidecar notebook** (`.re_notes.json` next to the binary) that stores facts, hypotheses, unknowns, and a step-by-step timeline. This notebook is injected into the system prompt each turn so Claude never loses context between tool calls.

---

## Setup

### Option A: With Binary Ninja (live analysis)

1. Copy `bn_bridge/` into your Binary Ninja plugins directory:
   - macOS: `~/Library/Application Support/Binary Ninja/plugins/re_agent/`
   - Linux: `~/.binaryninja/plugins/re_agent/`
   - Windows: `%APPDATA%\Binary Ninja\plugins\re_agent\`

2. Open a binary in Binary Ninja. The bridge starts automatically on `:7734`.  
   You can also start it manually: **Plugins → RE Agent → Start Bridge Server**

3. Install agent dependencies and run:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
python run_agent.py --bridge http://127.0.0.1:7734
```

### Option B: Without Binary Ninja (mock mode)

A synthetic stealer sample is built in — no BN needed to develop or test the agent.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
python run_agent.py --mock
```

The mock simulates a Chrome credential stealer: `sqlite3_open` → `CryptUnprotectData` → `HttpSendRequestA` → `/gate.php`. The agent will find the collection path, the DPAPI decryption, and the HTTP exfil in roughly 5 phases.

---

## Usage

```bash
# Full 5-phase malware triage (mock)
python run_agent.py --mock

# Full triage against live BN session
python run_agent.py --bridge http://127.0.0.1:7734

# Single-objective investigation
python run_agent.py --mock --oneshot "find the C2 communication path"
python run_agent.py --mock --oneshot "determine whether this binary decrypts an embedded config"

# Run specific phases only
python run_agent.py --mock --phases 1,2

# Quiet mode — suppress per-turn output, show final report only
python run_agent.py --mock --quiet

# Adjust turn budget per phase (default: 12)
python run_agent.py --mock --max-turns 20
```

---

## Agent tools

The agent has 17 tools wired to the bridge:

| Tool | What it does |
|------|-------------|
| `get_session` | Current binary, arch, platform, open function |
| `get_overview` | Imports, sections, strings stats, capability tags |
| `list_functions` | Ranked function list with suspicion scores |
| `search_strings` | Keyword/regex search over all strings |
| `search_imports` | Search imported symbols by name/module |
| `find_references` | All code refs to a string, import, or address |
| `get_function_summary` | Signature, stack vars, constants, callers, callees |
| `decompile_function` | HLIL / MLIL / LLIL / disasm (bounded by max_lines) |
| `get_callers` | Functions that call this one |
| `get_callees` | Functions called by this one |
| `get_xrefs` | Code and data references to an address |
| `rank_functions` | Top N functions for a given objective |
| `write_note` | Record fact / hypothesis / unknown / todo |
| `rename_symbol` | Rename a function or symbol (preview-first) |
| `set_comment` | Set a comment at an address (preview-first) |
| `tag_function` | Tag a function with a label (preview-first) |
| `patch_preview` | Preview a binary patch without applying it |

All mutation tools (`rename_symbol`, `set_comment`, `tag_function`, `patch_preview`) default to `preview=true`. Nothing is committed to the Binary Ninja database without a second explicit call — the agent can propose changes, you decide whether to apply them.

---

## Suspicion scoring

Functions are ranked before the agent deep-dives any of them:

```
score =
  3 × network import hit      (connect, send, InternetOpen, HttpSend, …)
  3 × crypto import hit       (CryptUnprotectData, BCrypt, RC4, AES, …)
  2 × credential string hit   (Login Data, password, os_crypt, sqlite3_open, …)
  2 × anti-debug import       (IsDebuggerPresent, CheckRemoteDebugger, …)
  2 × high out-degree         (≥6 callees)
  1 × high centrality         (≥3 callers)
  1 × large function          (≥400 bytes)
  0.5 × complex CFG           (≥15 basic blocks)
  +1 bonus: network + credential combo
```

---

## Notebook schema

Every finding is stored in a sidecar file (`<binary>.re_notes.json`) next to the binary:

```json
{
  "binary_id": "sample.exe::sha256:aabbccdd",
  "objective": "determine what this binary does",
  "facts": [],
  "hypotheses": [],
  "unknowns": [],
  "timeline": [
    {
      "step": 1,
      "action": "inspect_function",
      "target": "0x407820",
      "reason": "top ranked — chrome credential strings + sqlite imports",
      "outcome": "confirmed login db access"
    }
  ],
  "function_notes": {
    "0x407820": [
      {
        "kind": "fact",
        "text": "opens Chrome Login Data sqlite database and queries credential rows",
        "evidence": ["sqlite3_open", "Login Data string", "SELECT ... FROM logins"],
        "confidence": 0.98
      }
    ]
  }
}
```

---

## Triage workflow

The 5-phase loop in `agent/loops/triage.py`:

**Phase 1 — Entry scan**  
`get_overview` → tag imports by capability cluster (network / crypto / credential / registry / anti_debug) → detect packing from section entropy

**Phase 2 — Function clustering**  
`rank_functions` → `get_function_summary` for top 5 → group into init / config / crypto / network / persistence / anti_analysis

**Phase 3 — Main path reconstruction**  
Decompile priority function → pull callers/callees → trace the behavior chain (e.g. collect → serialize → encrypt → exfil) → write provisional renames (`maybe_collect_logins`)

**Phase 4 — Hypothesis verification**  
`find_references` to confirm data flow → retry with MLIL/LLIL when HLIL misleads → check constants for port numbers, XOR keys, magic bytes

**Phase 5 — Structured conclusion**

```
TRIAGE REPORT
=============
BINARY: sample_stealer.exe
LIKELY TYPE: stealer

CONFIRMED BEHAVIOR:
  • sub_407820 enumerates Chrome Login Data via sqlite3 — evidence: sqlite3_open, Login Data string, SELECT query
  • sub_408d10 decrypts DPAPI-protected credential blobs — evidence: CryptUnprotectData, DATA_BLOB setup
  • sub_405120 exfiltrates data via HTTP POST to /gate.php — evidence: HttpSendRequestA, Mozilla/5.0 UA, /gate.php string

KEY FUNCTIONS:
  • 0x407820  collect_chrome_logins  — enumerate + extract credential rows
  • 0x408d10  decrypt_dpapi_blob     — DPAPI decryption wrapper
  • 0x405120  http_exfil             — outbound POST to C2

UNCERTAINTY:
  • Persistence path not yet confirmed — no registry/service writes found in static analysis
  • Whether additional browser families are targeted

NEXT BEST ACTIONS:
  • Dynamic run with API monitor — watch for additional CryptUnprotectData calls
  • Dump decrypted config buffer at sub_401500 output
  • Check for mutex / named pipe that would indicate C2 keepalive
```

---

## Project layout

```
re-agent/
├── bn_bridge/
│   ├── plugin.py       BN plugin — HTTP bridge server (drop into BN plugins dir)
│   ├── inspectors.py   All read-only BN API operations
│   ├── mutation.py     Write operations (rename / comment / tag / patch)
│   ├── notes.py        Per-binary JSON sidecar notebook
│   ├── ranking.py      Suspicion score formula
│   └── mock.py         Synthetic binary session — no BN required
├── agent/
│   ├── client.py       REAgent — Claude tool-calling loop
│   ├── notebook.py     Notebook client (injected into system prompt)
│   └── loops/
│       └── triage.py   5-phase malware triage loop
├── prompts/
│   ├── system.txt          Core agent rules + output contract
│   └── malware_triage.txt  Triage phase guidance + priority scoring
├── requirements.txt
└── run_agent.py        CLI entry point
```

---

## Requirements

- Python 3.9+
- `anthropic >= 0.40.0`
- `requests >= 2.32.0`
- Binary Ninja (optional — mock mode works without it)
- `ANTHROPIC_API_KEY` environment variable
