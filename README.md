# MCP Knowledge Base and Code Execution Server

A Model Context Protocol (MCP) server built in Python that exposes a cloud-backed RAG (Retrieval-Augmented Generation) knowledge base and a fully sandboxed Python code execution environment to any MCP-compatible LLM client.

---

## Table of Contents

- [MCP Knowledge Base and Code Execution Server](#mcp-knowledge-base-and-code-execution-server)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Architecture](#architecture)
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Running the Server](#running-the-server)
    - [Standalone (for testing)](#standalone-for-testing)
    - [With Claude Desktop](#with-claude-desktop)
  - [Exposed Tools](#exposed-tools)
    - [`calculate_area` (toy)](#calculate_area-toy)
    - [`greet_user` (toy)](#greet_user-toy)
    - [`optimize_search_query`](#optimize_search_query)
    - [`upload_local_folder_to_cloud`](#upload_local_folder_to_cloud)
    - [`query_cloud_knowledge_base`](#query_cloud_knowledge_base)
    - [`install_libs_sandbox`](#install_libs_sandbox)
    - [`execute_code_sandbox`](#execute_code_sandbox)
  - [Security Model](#security-model)
  - [Design Decisions](#design-decisions)
  - [Known Limitations](#known-limitations)
  - [Project Structure](#project-structure)

---

## Overview

This server implements the [Model Context Protocol](https://modelcontextprotocol.io/) over stdio transport, enabling LLM clients (such as Claude Desktop) to call structured tools at runtime. The server provides two primary capabilities:

1. **Cloud RAG Pipeline** — Documents stored locally can be uploaded to a Pinecone Assistant cloud index. The LLM can then query that index to retrieve semantically relevant excerpts from those documents before composing its response.

2. **Sandboxed Code Execution** — The LLM can request execution of arbitrary Python code inside a resource-constrained Docker container. The execution environment is network-isolated, memory-capped, and CPU-throttled. Libraries required by the code can be pre-installed into a persistent cache volume, preventing redundant downloads.

---

## Architecture

```
LLM Client (e.g. Claude Desktop)
        |
        | JSON-RPC 2.0 over stdio
        v
+-------------------------+
|      MCP Server         |  <-- server.py
|  (Async I/O Event Loop) |
+-------------------------+
        |           |
        |           +-----> Pinecone AsyncPinecone Client
        |                        |
        |                   Pinecone Cloud
        |                   (RAG Assistant / Vector Store)
        |
        +-----> Docker Engine (local daemon)
                    |
               python:3.11-alpine container
                    |
               Sandboxed script execution
                    |
               Persistent PKG cache (host volume)
```

The server runs a single `asyncio` event loop. All blocking operations — Docker container lifecycle management, file writes, package installation — are dispatched to a thread pool via `loop.run_in_executor`, keeping the stdio I/O channel non-blocking at all times.

---

## Features

- **MCP-compliant JSON-RPC 2.0 server** over stdin/stdout transport
- **Automatic tool schema generation** from Python type hints via `inspect.signature`
- **Pinecone cloud RAG integration** — upload, index, and query documents
- **Two-phase Docker sandbox** — separate library installation and code execution containers
- **Persistent package cache** — libraries installed once, reused across executions via a host-mounted volume
- **Strict resource constraints** on execution containers: 512 MB RAM, 10% CPU, 10 PID limit, 30-second wall-clock timeout, no network access
- **Async stdout writer with drain()** to prevent EAGAIN / Errno 35 errors on macOS under high throughput
- **Concurrent request handling** via `asyncio.create_task` — multiple tool calls can be in-flight simultaneously

---

## Prerequisites

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| Docker Desktop | Any recent | Container sandbox |
| Pinecone account | — | Cloud vector store |
| `pinecone` SDK | v9.x | Async Pinecone client |
| `aiohttp` | Any | HTTP async support |
| `python-dotenv` | Any | Environment variable loading |
| `docker` (Python SDK) | Any | Docker Engine interaction |

Docker Desktop must be running on the host machine before starting the server. The server communicates with the Docker daemon via the default socket.

---

## Installation

**1. Clone the repository**

```bash
git clone <repository-url>
cd <repository-directory>
```

**2. Create and activate a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install Python dependencies**

```bash
pip install pinecone aiohttp python-dotenv docker
```

**4. Verify Docker is accessible**

```bash
docker info
```

If this command fails, ensure Docker Desktop is running.

---

## Configuration

Create a `.env` file in the project root:

```env
PINECONE_API_KEY=your_pinecone_api_key_here
```

The server reads this key at startup. If `PINECONE_API_KEY` is absent, the server will raise a `ValueError` and refuse to start.

**Sandbox directories** are created automatically at `~/.mcp_sandbox_cache/` on first run:

```
~/.mcp_sandbox_cache/
    site-packages/   # Persistent Python package cache (host-mounted into containers)
    workspace/       # Temporary script staging area
```

These paths are defined at the top of `server.py` and can be changed if required:

```python
SANDBOX_ROOT = os.path.expanduser("~/.mcp_sandbox_cache")
PKG_DIR      = os.path.join(SANDBOX_ROOT, "site-packages")
WORKSPACE_DIR = os.path.join(SANDBOX_ROOT, "workspace")
```

---

## Running the Server

### Standalone (for testing)

```bash
python server.py
```

The server will print initialization status to `stderr` and then block, listening for JSON-RPC messages on `stdin`.

### With Claude Desktop

Add an entry to your Claude Desktop MCP configuration file (typically `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "my-scratch-server": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

Restart Claude Desktop. The server will be spawned automatically as a subprocess and will communicate with the client over stdio.

---

## Exposed Tools

The server registers the following tools, each automatically discoverable by the MCP client via the `tools/list` method.

---

### `calculate_area` (toy)

Calculates the area of a rectangle.

| Parameter | Type | Required |
|---|---|---|
| `width` | integer | Yes |
| `height` | integer | Yes |

**Returns:** Integer area value.

---

### `greet_user` (toy)

Greets a user with either a formal or informal salutation.

| Parameter | Type | Required |
|---|---|---|
| `name` | string | Yes |
| `formal` | boolean | Yes |

**Returns:** Greeting string.

---

### `optimize_search_query`

Transforms a raw conversational user prompt into a clean, keyword-optimized search string suitable for vector database retrieval. The LLM invokes this tool when it needs to query the knowledge base with higher precision.

| Parameter | Type | Required |
|---|---|---|
| `raw_user_prompt` | string | Yes |

**Returns:** Optimized query string (prompt passed to the LLM for reformulation).

---

### `upload_local_folder_to_cloud`

Scans a local directory and uploads all `.pdf`, `.txt`, `.md`, and `.json` files to the Pinecone cloud assistant index. Files are chunked and embedded by Pinecone's infrastructure automatically.

| Parameter | Type | Required |
|---|---|---|
| `folder_path` | string | Yes |

**Returns:** Status report listing successfully uploaded files and any errors encountered.

---

### `query_cloud_knowledge_base`

Executes a semantic search against the Pinecone cloud assistant index and returns the top 4 most relevant document excerpts along with their source file names.

| Parameter | Type | Required |
|---|---|---|
| `query` | string | Yes |

**Returns:** Formatted string containing extracted document snippets with source labels.

---

### `install_libs_sandbox`

Installs one or more third-party Python packages into the persistent sandbox package cache using `pip` inside a temporary Docker container. This must be called before `execute_code_sandbox` if the script requires libraries not available in the Python standard library.

| Parameter | Type | Required |
|---|---|---|
| `libraries` | array (of strings) | Yes |

**Example:** `["numpy", "pandas"]`

**Returns:** Success or failure message from the installer container.

**Container constraints during install:** 512 MB RAM, 1 CPU core, network enabled (required for PyPI access), auto-removed after completion.

---

### `execute_code_sandbox`

Executes a Python script string inside a fully isolated Docker container. The container has no network access, is memory-capped, CPU-throttled, and subject to a hard 30-second execution timeout. Pre-installed libraries (via `install_libs_sandbox`) are available for import.

| Parameter | Type | Required |
|---|---|---|
| `code` | string | Yes |

**Returns:** A structured execution report containing process status, exit code, and combined stdout/stderr output.

**Container constraints during execution:**

| Resource | Limit |
|---|---|
| Memory | 512 MB |
| CPU | 10% of one core (100,000,000 nano CPUs) |
| PID limit | 10 |
| Network | Disabled |
| Wall-clock timeout | 30 seconds |
| Filesystem write access | None (all mounts are read-only) |

---

## Security Model

The sandbox is designed around a principle of minimal trust toward both the executing code and the LLM issuing the request.

**Network isolation.** The execution container has `network_disabled=True`. No outbound or inbound connections are possible from within a running script.

**Read-only mounts.** Both the package cache directory and the workspace directory are mounted into the execution container as read-only (`mode: "ro"`). Code running inside the container cannot modify, delete, or create files on the host filesystem.

**Resource ceilings.** Hard limits on memory (`512m`), CPU (`nano_cpus`), and process count (`pids_limit=10`) prevent denial-of-service scenarios such as fork bombs, memory exhaustion, or CPU starvation.

**Forced timeout and kill.** If a container exceeds 30 seconds, it is forcibly killed via `container.kill()` and then removed. No runaway process can persist beyond this window.

**Ephemeral containers.** Every execution spawns a fresh, clean container image (`python:3.11-alpine`). No state persists between executions in the container layer itself. The only shared state is the read-only package cache, which contains only pip-installed packages and no user data.

**Separation of install and execute phases.** Library installation uses a separate container with network access enabled but with its own resource limits. The execution container is always network-disabled, regardless of what libraries are present.

---

## Design Decisions

**Why `AsyncPinecone` instead of the synchronous client?**
The server is built entirely on `asyncio`. Using the synchronous `Pinecone` client would block the event loop during cloud API calls, stalling all concurrent tool calls and the stdio read loop. The async client allows cloud operations to yield control back to the loop while waiting for network I/O.

**Why `loop.run_in_executor` for Docker operations?**
The `docker` Python SDK is synchronous. Running it directly on the event loop would block the server. Dispatching Docker calls to a thread pool executor allows the async loop to continue processing other incoming requests while a container is being booted or awaited.

**Why `detach=True` in the execution container?**
With `detach=True`, the call to `client.containers.run()` returns immediately with a container object rather than blocking until the process completes. This allows the server to call `container.wait(timeout=30)` separately, which is required to implement the timeout and subsequent forced kill in a clean way.

**Why a persistent package cache on the host?**
Without persistence, every call to `execute_code_sandbox` that requires third-party libraries would need to download and install those libraries from PyPI inside the container before running the script. This adds significant latency (tens of seconds for larger packages such as NumPy or Pandas) to every execution. By installing once via `install_libs_sandbox` into a host directory and mounting it read-only into subsequent execution containers, packages are available immediately via `PYTHONPATH=/cache`.

**Why `await async_writer.drain()` in `send_response`?**
On macOS, the stdout pipe has a finite kernel buffer. If the server writes faster than the client reads, the buffer fills and subsequent writes raise `BlockingIOError` (Errno 35, EAGAIN). Calling `drain()` after each write yields to the event loop and waits for the OS buffer to clear before the next write proceeds. The `stdout_lock` additionally ensures that concurrent tasks do not interleave their JSON output on the stream.

**Why print all server logs to `stderr`?**
The MCP protocol uses stdin and stdout exclusively for JSON-RPC message exchange. Any plain text written to stdout will be received by the client's JSON parser and cause a parse failure. All diagnostic output, initialization messages, and trace logs are therefore directed to `stderr`, which is visible in the terminal but does not interfere with the protocol stream.

---

## Known Limitations

- The `python:3.11-alpine` image must be available locally or pullable from Docker Hub on first use. In air-gapped environments, the image must be pre-loaded manually.
- The `install_libs_sandbox` tool requires network access to PyPI. Packages that depend on compiled C extensions may fail to install on Alpine Linux if the required build tools (`gcc`, `musl-dev`) are not present in the image.
- The 30-second execution timeout may be insufficient for computationally intensive workloads. It can be adjusted by modifying the `timeout` argument passed to `container.wait()` in `execute_code_sandbox`.
- The Pinecone assistant name is hardcoded as `"my-notes"`. If multiple knowledge bases are required, this should be parameterized.
- There is no authentication or authorization layer on the MCP server itself. Access control is entirely delegated to the MCP client (Claude Desktop). Do not expose this server over a network interface.

---

## Project Structure

```
.
├── server.py          # Main MCP server — tool definitions, JSON-RPC handler, async I/O loop
├── .env               # Environment variables (not committed to version control)
└── README.md          # This document
```

The sandbox cache is created at runtime outside the project directory:

```
~/.mcp_sandbox_cache/
    site-packages/     # Pip-installed packages (persistent across server restarts)
    workspace/         # Staging area for sandbox scripts (cleaned after each execution)
```

---

