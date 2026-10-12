# Verification

The system is not considered finished until it passes end-to-end, recovery and security
tests. This is what currently runs, and what it actually exercises.

```
$ python -m pytest
122 passed in 6.80s
```

| Suite | File | What it proves |
|---|---|---|
| End to end | `tests/test_operating_cycle.py` | A request walks all nine phases, checkpoints are written in order, decisions are auditable, results are verified, and work with no success criteria is **blocked**, not marked complete. |
| Recovery | `tests/test_recovery.py` | Transient failures retry; a timeout triggers the `extend_timeout` repair against a real subprocess; the attempt budget is bounded; permission refusals are terminal; an optional step failing does not block the task; the emergency stop preserves state and work resumes. |
| Security | `tests/test_security_permissions.py` | Mode/scope/risk rules, approvals (park → approve → complete, deny → block, never asked twice), workspace path escape, destructive command refusal, dry-run desktop control, full audit trail. |
| Durability | `tests/test_persistence.py` | Tasks, goals and memory survive a restart; an interrupted task is re-queued; a parked approval survives and resumes; undispatched events replay; **a resumed task never re-runs completed steps**. |
| Goals | `tests/test_odysseus.py` | Milestones queue once, definition of done is verified independently, event monitors create proactive work, cooldowns prevent floods, cancellation cascades. |
| Self-evaluation | `tests/test_evolver.py` | Verified work scores higher than blocked work, postmortems are written, repeated failures produce proposals, proposals do nothing until approved, and out-of-allowlist proposals are refused. |
| Memory | `tests/test_memory.py` | Relevance ranking, determinism, goal boosting, importance, forgetting, pruning, episodes. |
| Models | `tests/test_router.py` | A real model is preferred over the rule engine, a failing model falls back, routing is recorded, preferences change ranking, the planner always yields an executable plan. |
| Interfaces | `tests/test_desktop_api.py`, `tests/test_voice.py`, `tests/test_cli.py` | The HTTP surface, the voice path and the CLI all drive the same runtime and report honest outcomes. |

## What is *not* verified here

* **The Windows build.** `jarvis/packaging/build_windows.ps1`, `jarvis.spec` and
  `installer.iss` are provided but were not executed: this environment is Linux, so no
  PyInstaller/Inno Setup run has happened. Run the script on Windows before shipping.
* **Real desktop control.** `computer.*` tools are tested in dry-run mode only. With
  `JARVIS_COMPUTER_CONTROL=1` and a display backend the same code path drives real input
  devices; that has not been exercised here.
* **Live local/cloud models.** Routing, fallback and JSON extraction are tested against
  stubs and the deterministic provider. No Ollama instance or cloud API was called.
* **Speech hardware.** Voice is tested with a text synthesizer and a scripted recognizer.
