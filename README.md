# Custom MCP Server (Async, Python)

A from-scratch implementation of a Model Context Protocol (MCP) server built on raw `asyncio`, JSON-RPC 2.0, and stdin/stdout transport — without relying on the official MCP SDK's server scaffolding. It exposes a set of tools to any MCP-compatible client (e.g. Claude Desktop, Claude Code) and integrates with Pinecone Assistant for cloud-based RAG, plus a sandboxed Docker code execution environment.

## Overview

The server implements the core MCP lifecycle (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`) over a JSON-RPC protocol carried on stdin/stdout. Tool registration is handled via a custom decorator that introspects Python type hints to auto-generate JSON Schema definitions, so tools are defined as plain async functions with type-annotated parameters and docstrings.

## Architecture

### Transport layer
- Reads JSON-RPC requests line-by-line from `stdin` using an `asyncio.StreamReader`.
- Each request is dispatched as an independent `asyncio.Task`, so multiple tool calls can be processed concurrently.
- Responses are written back to `stdout` via an `asyncio.StreamWriter`, guarded by an `asyncio.Lock` to prevent interleaved writes from concurrent tasks.
- All diagnostic/log output is written to `stderr`. This is required because the JSON-RPC protocol on stdout must remain free of any non-JSON text, or the client's parser will break.

### Tool registry
- The `MCPServer` class maintains a `tools_registry` (name → coroutine) and `tools_blueprints` (JSON Schema definitions for each tool).
- The `@server.tool()` decorator inspects each function's signature via `inspect.signature()`:
  - Maps Python type hints (`int`, `float`, `bool`, `dict`, `list`, default `string`) to JSON Schema types.
  - Parameters without default values are marked as `required`.
  - The function's docstring becomes the tool's `description`, which is what the LLM client reads to decide when to call the tool.

### JSON-RPC methods supported
| Method | Behavior |
|---|---|
| `initialize` | Returns protocol version, server capabilities, and server info. |
| `notifications/initialized` | Acknowledged silently (no response, per MCP spec for notifications). |
| `tools/list` | Returns all registered tool blueprints. |
| `tools/call` | Looks up the requested tool by name, executes it with the provided arguments, and returns its output as text content. |

Unknown methods and unknown tool names return standard JSON-RPC error objects (`-32601`).

## Tools

### `calculate_area(width: int, height: int)`
Returns the area of a rectangle. Included as a minimal sanity-check tool for verifying the schema-generation and dispatch pipeline.

### `greet_user(name: str, formal: bool)`
Returns a formal or informal greeting depending on the `formal` flag. Also serves as a simple example of boolean parameter handling.

### `optimize_search_query(raw_user_prompt: str)`
Does not perform a search itself. Instead, it returns a prompt instructing the calling LLM to strip conversational filler from a raw user query and produce a clean, keyword-optimized search string — intended to be used as an intermediate step before calling `query_cloud_knowledge_base`.

### `upload_local_folder_to_cloud(folder_path: str)`
Scans a local directory for `.pdf`, `.txt`, `.md`, and `.json` files and uploads each one to a Pinecone Assistant index for indexing and chunking. Returns a summary report of files successfully uploaded and any failures encountered.

### `query_cloud_knowledge_base(query: str)`
Queries the Pinecone Assistant's context endpoint (`top_k=4`) and returns the matched text snippets along with their source document names. Used to ground LLM responses in previously uploaded documents.

### `execute_code_sandbox(code: str)`
Executes arbitrary Python code inside an isolated, ephemeral Docker container, with the following safety constraints:
- Base image: `python:3.11-alpine`
- Read-only volume mount (the container cannot modify the host filesystem)
- No network access (`network_disabled=True`)
- 128 MB memory limit
- CPU capped at ~10% of one core (`nano_cpus`)
- Max 10 PIDs
- Hard 3-second execution timeout, after which the container is force-killed

The code is written to a temporary host directory, mounted read-only into the container, executed, and the combined stdout/stderr is captured and returned as a structured execution report. The temporary directory and container are always cleaned up, even on failure.

## Pinecone Initialization

On startup, the server connects to Pinecone using `PINECONE_API_KEY` (loaded from a `.env` file via `python-dotenv`) and checks whether an assistant named `my-notes` already exists:
- If not found, it creates a new Pinecone Assistant with instructions configured for precise, factual retrieval.
- A short delay is added after creation to account for the asynchronous provisioning of cloud resources.
- All status messages during this phase are printed to `stderr` to avoid corrupting the JSON-RPC stream on `stdout`.

## Requirements

- Python 3.10+ (uses `asyncio` stream APIs and modern type-hint introspection)
- Docker daemon running locally and accessible (used by `execute_code_sandbox`)
- A Pinecone account and API key with Assistant access

### Python dependencies
```
aiohttp
python-dotenv
pinecone
docker
```

### Environment setup
Create a `.env` file in the project root:
```
PINECONE_API_KEY=your_api_key_here
```

## Running the Server

The server communicates exclusively over stdin/stdout using JSON-RPC, so it is designed to be launched by an MCP-compatible client rather than run interactively. A typical client configuration entry would point to:

```
python server.py
```

On startup you should see initialization logs on `stderr` confirming:
1. Pinecone connectivity and assistant status
2. The async I/O parsing loop has started

## Notes and Known Considerations

- **Concurrency**: Because each incoming request is spawned as a separate task, tool calls can execute and respond out of order relative to when they were received. The stdout lock only guarantees writes don't interleave mid-message, not that responses are ordered.
- **Type schema limitations**: The decorator's type mapping only covers `int`, `float`, `bool`, `dict`, and `list`; anything else (including unannotated parameters) defaults to `string` in the generated schema.
- **Sandbox trust boundary**: The Docker sandbox mounts code read-only specifically so that code executed inside the container cannot write back to the host's temporary directory — the mount exposes the host file rather than a copy.

