# JARVIS — Complete Concept

JARVIS is a **persistent personal AI operating system and autonomous digital
assistant**.

It is a software system that runs on your computer, uses local and cloud AI models,
remembers relevant information, manages goals, operates tools and applications, and
executes tasks on your behalf within defined permissions.

Unlike a conventional chatbot, JARVIS maintains continuity between conversations and
manages work even when you are not actively chatting with it.

Its defining capabilities are:

1. Persistent memory and identity
2. Reactive **and** proactive operation
3. Computer control, coding, research and automation
4. Long-running goal management
5. Self-evaluation, error recovery and governed improvement
6. Independent verification of completed work

---

## 1. What it is

| Property | Meaning in this implementation |
|---|---|
| Persistent | All goals, tasks, events, decisions, checkpoints, approvals, verifications, memory and audit live in SQLite (`%APPDATA%\JARVIS\jarvis.db`). Nothing important lives only in process memory. |
| Personal | One runtime, one store, three surfaces (desktop, voice, CLI) that share it. |
| Operating system | A scheduler, a permission model, a tool layer and an executive that decides what runs next. |
| Autonomous | Work can start from an event or a schedule, not only from a message. |

## 2. How it works

```
                 You
   voice · text · persistent goal
                  │
                  ▼
        ┌───────────────────┐
        │  JARVIS Executive │  understands intent, manages goals,
        └─────────┬─────────┘  decides what happens next
                  │
     ┌────────────┼────────────────┐
     ▼            ▼                ▼
┌──────────┐ ┌──────────┐  ┌────────────────┐
│ AI       │ │ Execution│  │ Authorization  │
│ intel    │ │ tools    │  │ & verification │
│ router + │ │ computer │  │ policy engine, │
│ planner  │ │ code     │  │ approvals,     │
│          │ │ research │  │ independent    │
│          │ │          │  │ checks         │
└────┬─────┘ └────┬─────┘  └───────┬────────┘
     └────────────┼────────────────┘
                  ▼
     ┌────────────────────────────┐
     │ Persistent memory & runtime│  goals, events, decisions,
     │ (SQLite)                   │  checkpoints, history
     └────────────────────────────┘
```

### The operating cycle

Every task walks nine phases. Each phase is persisted **before** the next one starts,
so an interrupted run resumes where it stopped rather than starting over.

| # | Phase | What happens |
|---|---|---|
| 1 | `received` | A request arrives, or an event/schedule triggers work. |
| 2 | `retrieved` | Relevant memory is recalled; the owning goal's context is attached. |
| 3 | `objective_defined` | The objective is fixed *before* planning, so success can be judged independently of the method. |
| 4 | `planned` | The routed model proposes steps and success criteria; the plan is validated against the tool registry. |
| 5 | `authorized` | The policy engine checks every scope. Anything above the auto-approved risk level parks the task and asks you. |
| 6 | `executed` | Steps run through the tool layer; progress is persisted after each step. |
| 7 | `verified` | The verifier re-derives the result from the outside world. A tool's own claim is never accepted. |
| 8 | `repaired` | Recoverable failures are retried or repaired; anything else becomes a blocker with evidence. |
| 9 | `memory_updated` | The episode is remembered, the goal is re-scored, and other eligible work continues. |

### Subsystems

* **Executive** (`jarvis/core/executive.py`) — the cycle above, plus halt/resume and cancellation.
* **Intelligence** (`jarvis/intelligence/`) — provider-agnostic routing across local (Ollama),
  cloud (any OpenAI-compatible endpoint) and a deterministic rule engine. Selection is
  recorded as a decision, and a model failure falls back instead of failing the task.
* **Tools** (`jarvis/tools/`) — 25 built-in capabilities across files, shell/code, research,
  desktop control and automation. Every call goes through one registry, which is the single
  place scopes are checked, the emergency stop is honoured and the audit log is written.
* **Memory** (`jarvis/core/memory.py`) — facts, episodes, preferences and reflections with
  deterministic, explainable recall (lexical overlap + tags + goal match + importance/recency).
* **Permissions** (`jarvis/core/permissions.py`) — modes (`readonly` / `standard` /
  `autonomous`), capability scopes, five risk levels, persisted approvals, emergency stop.
* **Verification** (`jarvis/core/verification.py`) — success criteria evaluated from the
  outside world (file contents, command exit codes, computed values), with evidence stored.
* **Recovery** (`jarvis/core/recovery.py`) — failure classification, retry, repair and escalation.
* **Odysseus** (`jarvis/odysseus/`) — long-running goals: milestones, monitors, resume after
  restart, progress against a definition of done.
* **Evolver** (`jarvis/evolver/`) — self-evaluation, postmortems and improvement *proposals*.
  A proposal is inert until approved, and may only change configuration or planner guidance.
  Evolver cannot rewrite its own code.

## 3. How you use it

One assistant. You do not manage its internal agents, models or tools.

**Immediate request**

> "Investigate this bug, fix it, run the tests and show me the verified result."

```
jarvis ask "investigate this bug, fix it, run the tests and show me the verified result"
```

JARVIS performs the authorized task, verifies the result independently, and reports what
succeeded or failed — with the evidence.

**Long-term goal**

> "Manage this project until it reaches a tested release."

```
jarvis adopt-goal "Reach a tested release" "Ship with a green test suite" \
      --milestone "Inventory the workspace" --milestone "Run the test suite" \
      --check '{"kind":"command","args":{"cmd":"python -m pytest -q"},"expect":0}'
```

JARVIS keeps the goal between sessions, queues milestone work, reacts to events and
resumes safely after interruptions or restarts.

**Proactive operation**

> "Monitor my approved project checks and notify me if a meaningful failure occurs."

```
jarvis adopt-goal "Keep main green" "React to CI failures" \
      --monitor '{"name":"ci","trigger":"event","kind":"ci.failure","intent":"Investigate the CI failure"}'
```

JARVIS evaluates relevant events, initiates authorized diagnostic work and escalates
anything it cannot safely resolve.

**You stay in control** through task monitoring, approval requests, cancellation, memory
management and an emergency stop (`jarvis halt`, or the button in the desktop UI).

## 4. What makes it different

| Conventional chatbot | JARVIS |
|---|---|
| Responds primarily to messages | Responds *and* proactively manages goals |
| Context is conversation-bound | Persistent operational state and memory |
| Suggests what to do | Executes authorized work |
| May claim success from tool output | Requires task-specific, independently evaluated verification |
| Stops when the conversation ends | Resumes durable tasks after restarts |
| Tied to one model interface | Interchangeable local and cloud models, with an offline fallback |

## 5. Final end state

A running, installable Windows application with a shared executive runtime, desktop and
voice interfaces, hybrid intelligence, persistent memory, computer-control tools,
scheduling, long-running goal management, strict permissions and verified execution.

The system must pass end-to-end, recovery and security tests before it can be considered
finished. See `docs/VERIFICATION.md` for the current state of those suites.

## 6. Status — targets, not claims

These are **target capabilities**. This table states honestly what the code does today.

| Capability | Status | Where |
|---|---|---|
| Nine-phase operating cycle with persisted checkpoints | Implemented | `jarvis/core/executive.py` |
| Persistent memory with deterministic recall | Implemented | `jarvis/core/memory.py` |
| Interchangeable local/cloud model routing with fallback | Implemented | `jarvis/intelligence/` |
| Tool layer with per-call authorization and audit | Implemented | `jarvis/tools/` |
| Independent verification with stored evidence | Implemented | `jarvis/core/verification.py` |
| Recovery: classify, retry, repair, escalate | Implemented | `jarvis/core/recovery.py` |
| Long-running goals, milestones, resume after restart | Implemented (Odysseus) | `jarvis/odysseus/` |
| Proactive event/schedule monitors | Implemented | `jarvis/core/scheduler.py` |
| Self-evaluation and governed improvement proposals | Implemented (Evolver) | `jarvis/evolver/` |
| Desktop interface (local HTTP + monitoring UI) | Implemented | `jarvis/interfaces/desktop/` |
| CLI | Implemented | `jarvis/interfaces/cli.py` |
| Voice interface | Implemented, backend-dependent | `jarvis/interfaces/voice.py` |
| Computer control (keyboard/mouse/windows) | Implemented but **off by default**; dry-runs unless `JARVIS_COMPUTER_CONTROL=1` and a backend is present | `jarvis/tools/builtin/computer.py` |
| Autonomous code repair (writing the fix itself) | **Not implemented** — the plan and tools exist (`code.apply_patch`, `code.run_tests`), but there is no autonomous code-editing intelligence yet |
| Windows installer build | Scripts provided (`jarvis/packaging/`); **not built or run here** — this environment is Linux |
| Semantic (embedding-based) memory recall | Not implemented — recall is lexical by design, so it can be explained |
