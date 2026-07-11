# Background Task Queue for `my-scratch-server`

A self-healing, parallel background task queue built on top of the existing MCP sandbox tools. Claude can hand off multiple coding tasks at once, each runs in an isolated Docker sandbox, a cheap background LLM auto-retries failures without burning Claude's context window, and Claude can check progress or pull final results on demand — all without blocking the conversation.

> **Extends, doesn't replace.** The original `execute_code_sandbox` tool is untouched and still works exactly as before. This is a pure addition for parallel, unattended, self-healing execution.

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [New MCP tools](#new-mcp-tools)
- [Setup](#setup)
- [Usage](#usage)
- [Known limitations](#known-limitations)
- [Security: cross-task directory leak (fixed)](#security-cross-task-directory-leak-fixed)
- [Troubleshooting](#troubleshooting)
- [Resetting task history](#resetting-task-history)

---

## Why this exists

The base MCP server runs one script at a time, synchronously — Claude calls `execute_code_sandbox`, waits, gets a result. Retrying on failure means the *full* stack trace and every failed draft sit in Claude's conversation context.

This extension moves that retry loop into the background:

- **Parallel execution** — queue several independent coding tasks (e.g. "write a transformer" + "write a linear regression script") and they run concurrently, not one after another.
- **Context-efficient self-healing** — a cheaper/faster model attempts up to 3 fixes per task on its own. Only the final working code (or, if it never succeeds, the full failure history) ever reaches Claude's context — the noisy middle attempts are discarded.
- **Non-blocking** — `queue_coding_task` returns instantly with a task ID. Claude keeps chatting normally and checks back later.

---

## Architecture

```
Claude calls queue_coding_task(name, code)
        │
        ▼
MCP server writes {"status": "QUEUED"} to Redis, fires the Celery task,
returns the task_id immediately — does not block
        │
        ▼
Redis (broker, db 0) ──► Celery worker (separate process) picks it up
        │
        ▼
run_and_heal():
   ├─ run code in Docker sandbox (isolated, no network, resource-capped)
   │
   ├─ SUCCESS → save final code + short summary to Redis (db 2)
   │            failed attempts discarded — never touch Claude's context
   │
   └─ FAILED  → cheap LLM rewrites the code → retry
                (max 3 attempts, early-exit if the same error repeats twice)
```

| File | Role |
|---|---|
| `sandbox_runner.py` | Synchronous, standalone Docker sandbox execution — safe to call from a Celery task. Unique script filename per run so parallel executions never collide. |
| `celery_tasks.py` | Celery app + `run_and_heal` task: runs code, retries with a cheap fixer LLM on failure, persists results to Redis. |
| `server.py` (additions) | Four new MCP tools wiring the above into Claude. |

Two logical Redis databases, one Redis instance:
- **db 0** — Celery's internal broker/backend bookkeeping (untouched by you directly)
- **db 2** — task metadata: status, final code, retry history

The Docker package cache (`~/.mcp_sandbox_cache/site-packages`) is shared with the original `execute_code_sandbox` / `install_libs_sandbox` tools — no separate install step needed for queued tasks.

---

## New MCP tools

#### `queue_coding_task(task_name: str, code: str)`
Queues code to run in the background with automatic retry-healing. Returns a `task_id` immediately.

#### `list_active_tasks()`
Lightweight status overview of every task: `QUEUED`, `SUCCESS`, or `STUCK`.

#### `get_task_result(task_id: str)`
Full detail for one task — final working code on `SUCCESS`, or the complete attempt-by-attempt failure history on `STUCK`.

#### `clear_task_history(only_finished: bool = True)`
Clears stored task records. Defaults to clearing only finished (`SUCCESS`/`STUCK`) tasks; pass `False` to wipe everything, including in-progress/orphaned entries.

---

## Setup

### Prerequisites
- Docker
- Redis (via Docker is easiest)
- Python 3.11, with `celery`, `redis`, and your chosen fixer-LLM SDK installed in the project's venv

### 1. Start Redis
```bash
docker run -d -p 6379:6379 --name sandbox-redis redis
```

### 2. Add your API keys to `.env`
```env
ANTHROPIC_API_KEY=...
PINECONE_API_KEY=...
GEMINI_API_KEY=...   # or GOOGLE_API_KEY, depending on your google-genai SDK version
```

### 3. Start the Celery worker

Run this in a **plain terminal application** (e.g. macOS Terminal.app), not an editor's integrated terminal — closing an editor's terminal panel kills any process running inside it. Keep this terminal open and running in the background.

```bash
cd /path/to/repo
source .venv/bin/activate
celery -A celery_tasks worker --loglevel=info -n mcp_worker@%h
```

The `-n mcp_worker@%h` flag gives this worker a unique name, avoiding duplicate-node conflicts if you're running Celery for another project on the same machine.

### 4. Launch Claude Desktop
Your existing MCP configuration handles starting `server.py` — no changes needed there beyond the new tool definitions.

**You need all three (Redis, Celery worker, Claude Desktop) running simultaneously for the queue tools to work.**

---

## Usage

Ask Claude something like:

> "Write me a transformer from scratch and also a linear regression script — run both."

Claude calls `queue_coding_task` twice, gets two task IDs, and returns to normal conversation. Later:

> "How are those tasks doing?"

Claude calls `list_active_tasks()`, sees current statuses, and pulls full code/errors via `get_task_result()` only for the tasks worth looking at.

---

## Known limitations

- **No push notifications.** MCP is request/response only — Claude checks on tasks the next time it gets a turn to speak, not the instant a task finishes.
- **Code changes require a worker restart.** The worker imports `celery_tasks.py`/`sandbox_runner.py` once at startup; edits aren't picked up until you stop and re-run it.
- **No `RUNNING` state yet.** Tasks show `QUEUED` for their entire in-progress duration (can be 30–90+ seconds across retries). A task genuinely stuck forever usually means no worker is running — check first before assuming a bug.
- **Orphaned `QUEUED` entries** can occur if a worker dies mid-task. Clean up with `clear_task_history()`.

---

## Security: cross-task directory leak (fixed)

### The risk

`sandbox_runner.py` originally mounted the **entire `WORKSPACE_DIR`** into every sandbox container, not just the single script file being executed for that task:

```python
# BEFORE — vulnerable
volumes={
    PKG_DIR: {"bind": "/cache", "mode": "ro"},
    WORKSPACE_DIR: {"bind": "/workspace", "mode": "ro"}   # whole directory mounted
}
```

Giving each task a unique filename (`sandbox_{uuid}.py`) only controlled *which file the container's command executed* — it did nothing to restrict what the container could *see*. Any code running inside a sandbox had full read access to the entire workspace directory, meaning it could enumerate and read every other task's script currently sitting on disk:

```python
# code inside ANY task's container could run this:
import os
for f in os.listdir("/workspace"):
    print(open(f"/workspace/{f}").read())
```

This mattered most for tasks running **concurrently** (multiple scripts genuinely present in the directory at the same time), and for any script left behind by a failed cleanup (e.g. a crash before the `finally` block's `os.remove()` ran). It defeated part of the actual point of sandboxing: `network_disabled`, `mem_limit`, and `pids_limit` constrain what code can *do*, but this left a wide-open read channel into what code could *see* — including code it had no business seeing.

### The fix

Mount the **specific host file** for that run to a fixed in-container path, instead of mounting the shared directory:

```python
# AFTER — fixed
def run_code_in_sandbox(code: str) -> tuple[int, str]:
    script_name = f"sandbox_{uuid.uuid4().hex[:8]}.py"
    script_path = os.path.join(WORKSPACE_DIR, script_name)

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    container = client.containers.run(
        image="python:3.11-alpine",
        command=["python", "/workspace/script.py"],           # fixed in-container name
        volumes={
            PKG_DIR: {"bind": "/cache", "mode": "ro"},
            script_path: {"bind": "/workspace/script.py", "mode": "ro"}  # single FILE, not the dir
        },
        ...
    )
```

Each container now gets its own private `/workspace/script.py`, bind-mounted to a different host file per task — no shared directory is ever exposed inside any container, so no task can enumerate or read another task's code. This restores the same single-file mount pattern the original `execute_code_sandbox` tool already used, just made compatible with parallel, uniquely-named files.

### Verification

Three adversarial probe scripts were queued through `queue_coding_task` post-fix to confirm the boundary actually holds:

| Probe | Result |
|---|---|
| `os.listdir("/workspace")` | Returns only `['script.py']` — the task's own file, not other tasks' scripts |
| Guess common paths (`/workspace/sandbox_script.py`, `/app/script.py`, etc.) | All correctly report "does not exist" |
| Write to `/workspace/script.py` | Correctly blocked: `Read-only file system` |
| Outbound network connection | Correctly blocked: `Network unreachable` |

Note: `/cache` (the shared package cache) remains visible and shared across all tasks — this is **intentional**, not a leak. It exists specifically so `pip`-installed packages (numpy, pandas, etc.) are available to every task without re-installing, and it never contains task-specific code or data.

**Recommended as a standing regression check:** re-run this same probe set any time `sandbox_runner.py`'s volume/mount configuration is touched in the future.

---



## Troubleshooting

**Symptom: a task stays `QUEUED` forever.**

1. Confirm a worker is actually running and healthy:
   ```bash
   celery -A celery_tasks inspect active
   ```
   Should report exactly `1 node online`, with no duplicate-node warning.

2. Check for stray/duplicate workers from other projects:
   ```bash
   ps aux | grep celery
   ```
   Kill any pointing at a different project directory:
   ```bash
   kill -9 <PID>
   # or, to target by command substring:
   pkill -9 -f "celery -A <other_app_name>"
   ```

3. Confirm Docker is actually executing containers while a task is in flight:
   ```bash
   docker ps
   ```

4. Inspect the raw Redis record directly, bypassing the MCP layer:
   ```python
   import redis
   r = redis.Redis(host="localhost", port=6379, db=2, decode_responses=True)
   print(r.get("task:<task_id>"))
   ```

5. Check whether messages are stuck unconsumed in the broker queue:
   ```bash
   redis-cli -n 0 llen celery
   ```
   A nonzero, non-shrinking count with a healthy worker running usually means a startup error in the worker — check its log for something like a `ModuleNotFoundError` (commonly caused by starting the worker from the wrong directory).

---

## Resetting task history

```bash
redis-cli -n 2 flushdb
```
Clears all stored task records (db 2 only). Does not affect Celery's broker state (db 0) or anything currently executing inside Docker.