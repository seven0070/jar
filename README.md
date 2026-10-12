# JARVIS

**Persistent personal AI operating system and autonomous digital assistant.**

JARVIS runs on your computer, uses local and cloud AI models, remembers relevant
information, manages goals, operates tools, and executes work on your behalf inside
permissions you control. It keeps working between conversations, and it verifies its
own results instead of claiming success.

Read **[CONCEPT.md](CONCEPT.md)** for the complete product definition, operating model
and user experience. This file is the quick start.

---

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[desktop,dev]"

# one assistant, three surfaces
.venv/bin/jarvis status                       # what is running, what is allowed
.venv/bin/jarvis ask "run the tests and show me the verified result"
.venv/bin/jarvis-desktop --host 127.0.0.1     # desktop interface on :8765
.venv/bin/python -m jarvis.interfaces.voice --text   # voice interface (typed fallback)
```

State lives in `%APPDATA%\JARVIS` on Windows and `~/.local/share/jarvis` elsewhere.
Override with `JARVIS_HOME`; set the directory JARVIS may act on with `JARVIS_WORKSPACE`.

## The three ways you interact with it

**Immediate request**

```bash
jarvis ask "investigate this bug, fix it, run the tests and show me the result"
```

**Long-term goal** — kept between sessions, resumed after restarts

```bash
jarvis adopt-goal "Reach a tested release" "Ship with a green test suite" \
      --milestone "Inventory the workspace" --milestone "Run the test suite" \
      --check '{"kind":"command","args":{"cmd":"python -m pytest -q"},"expect":0}'
jarvis advance            # one supervision pass
jarvis goal <goal-id>     # honest progress against the definition of done
```

**Proactive operation** — reacts to events you did not send

```bash
jarvis adopt-goal "Keep main green" "React to CI failures" \
      --monitor '{"name":"ci","trigger":"event","kind":"ci.failure","intent":"Investigate the CI failure"}'
jarvis event ci.failure --payload '{"run":42}'
```

## Staying in control

| Command | Effect |
|---|---|
| `jarvis approvals` / `jarvis approve <id> yes` | See and decide what needs your permission |
| `jarvis halt` / `jarvis resume` | Emergency stop; state is preserved and work resumes |
| `jarvis cancel <task-id>` | Stop a task |
| `jarvis --mode readonly\|standard\|autonomous` | Change the permission posture |
| `jarvis memories` / `recall` / `forget <id>` | Manage what JARVIS remembers |
| `jarvis audit` | Every action taken, with who authorized it |
| `jarvis review` / `proposals` / `decide-proposal <id> yes` | Self-evaluation and governed improvement |

Nothing above the auto-approved risk level runs without an explicit approval, and
`fs.delete`, publishing and self-modification are refused outright in standard mode.

## Models

JARVIS works with no keys and no network — a deterministic rule engine always answers.
Add real models when you want them:

```bash
set JARVIS_OLLAMA=llama3.1                       # local model (Windows)
set JARVIS_OPENAI_API_KEY=sk-...                 # any OpenAI-compatible endpoint
set JARVIS_OPENAI_MODEL=gpt-4o-mini
set JARVIS_MODEL_PREFERENCE=balanced             # balanced | cheap | fast | strong
```

Model selection is recorded as a decision, and a model that fails falls back to the next
one rather than failing your task.

## Windows application

```powershell
pwsh -File jarvis\packaging\build_windows.ps1
```

Runs the test suite first (a build is refused if it fails), produces a self-contained
`dist\JARVIS\JARVIS.exe` via PyInstaller (`jarvis/packaging/jarvis.spec`), and optionally
an installer with Inno Setup (`jarvis/packaging/installer.iss`).

## Tests

```bash
.venv/bin/python -m pytest
```

122 tests covering the operating cycle end to end, recovery and repair, permissions and
sandboxing, durability across restarts, long-running goals, self-evaluation, model
routing, the desktop API, voice and CLI. See `docs/VERIFICATION.md`.

## Layout

```
jarvis/
  core/           executive, store, memory, permissions, verification, recovery, scheduler
  intelligence/   providers (local/cloud/deterministic), router, planner
  tools/          registry + built-ins: files, shell/code, research, computer, automation
  odysseus/       long-running goal management
  evolver/        self-evaluation and governed improvement
  interfaces/     desktop (FastAPI + UI), voice, CLI
  packaging/      PyInstaller spec, Inno Setup script, Windows build script
```
