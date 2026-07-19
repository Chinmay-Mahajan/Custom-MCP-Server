# MCP Semantic Cache Proxy

A transparent caching layer for MCP (Model Context Protocol) tool calls. It sits between an LLM client (e.g. Claude) and a real MCP server, intercepts `tools/call` requests, and short-circuits any request that is **semantically similar** to a previous one — returning the cached response instead of re-executing the tool.

This is useful for tools that are slow, expensive, or hit external APIs (RAG lookups, code sandboxes, cloud uploads, etc.), where near-duplicate natural-language queries would otherwise trigger redundant work.

Caching is **opt-in per tool**, declared where the tool is defined on the real server. This matters because not every tool is safe to cache — see [Cache eligibility](#cache-eligibility-per-tool-opt-in) below.

## How it works

```
Client (Claude) <──stdio──> proxy_gateway.py <──stdio (subprocess)──> server.py (real MCP tools)
                                    │
                                    ▼
                              cache_engine.py ──> Redis
```

`proxy_gateway.py` spawns the real MCP server (`server.py`) as a child process and pipes `stdin`/`stdout` through itself, acting as a man-in-the-middle on the JSON-RPC stream:

0. **Handshake:** On `initialize`, `server.py` reports which of its own tools are cache-eligible via `serverInfo.meta.cacheable_tools`. The proxy reads this once, builds `CACHED_TOOLS_WHITELIST`, and strips the `meta` field back out before forwarding the response to the client (it's proxy-internal bookkeeping, not something the client needs to see).
1. **Inbound (client → server):** Every `tools/call` request is checked against the whitelist first.
   - **Not whitelisted:** forwarded straight to `server.py`, no embedding, no cache lookup — zero overhead for non-cacheable tools.
   - **Whitelisted:** the proxy extracts a representative text string from the arguments (`query`, `prompt`, `code`, `folder_path`, etc., falling back to the full serialized arguments), embeds it, and checks the semantic cache.
     - **Cache hit:** the proxy fabricates a JSON-RPC response itself, writes it directly to `stdout`, and **drops the request** — it never reaches `server.py`.
     - **Cache miss:** the request is forwarded to `server.py` as normal, and its `id` is stashed in `pending_cache_writes` along with the query text/vector so the response can be indexed later.
2. **Outbound (server → client):** Responses from `server.py` are inspected. If the response `id` matches a pending cache-miss entry, and the result **is not an error** (`isError` is checked), the result text is written into Redis (as a fire-and-forget background task) before being forwarded on to the client.

Every packet is also mirrored to a local `cache_debug.log` for auditing.

## Cache eligibility: per-tool opt-in

Earlier versions of this proxy cached every tool call indiscriminately by matching on argument text — which is unsafe for tools with side effects (see [Known limitations](#notes--limitations)). Caching is now an explicit decision made on the server side, not inferred by the proxy.

On `server.py`, the `@server.tool()` decorator takes a `cache_able` flag:

```python
@server.tool(False)
async def calculate_area(width: int, height: int):
    ...

@server.tool(True)  # safe to cache — same folder path + no changes = same result we already have
async def upload_local_folder_to_cloud(folder_path: str):
    ...
```

Every function decorated with `cache_able=True` is added to `server.cache_tools`, which gets advertised to the proxy at `initialize` time as `cacheable_tools`. The proxy trusts this list completely — it never guesses.

**Rule of thumb used in this codebase:**
- Cache-eligible: idempotent lookups, and writes where a repeated identical call is genuinely wasted work (e.g. re-uploading a folder whose contents haven't changed avoids burning ingestion tokens).
- Not cache-eligible: anything where "same input text" doesn't imply "same correct output" — destructive/stateful operations like `administrative_clear_cache`, `clear_task_history`, code execution, or tools whose result depends on external state that can change between calls with identical arguments.

## Cache invalidation

`administrative_clear_cache` is a (non-cacheable, by design) tool the LLM can call to flush the entire semantic cache — all `turn:*` hashes, `session:*:bucket:*` sets, and the `global:turn:counter` — via `clear_semantic_cache()` in `proxy_gateway.py`. Useful when cached responses go stale or start producing misleading hits (e.g. after uploading new files to a previously-cached folder).

## Semantic matching: LSH + cosine similarity

Naively comparing a new query's embedding against every cached vector via cosine similarity doesn't scale. Instead, `cache_engine.py` uses **Locality-Sensitive Hashing (LSH)** to narrow the search space before doing any expensive comparisons:

1. At startup, `NUM_PLANES` (8) random unit vectors ("hyperplanes") are generated in the same dimensionality as the embedding model's output (384, for `all-MiniLM-L6-v2`), seeded for reproducibility.
2. For any embedding, the dot product is taken against each of the 8 hyperplanes. Each dot product is thresholded at zero (positive → `1`, negative → `0`), producing an 8-bit string.
3. This bitstring identifies one of 2⁸ = 256 **buckets**, each roughly corresponding to a region of the embedding space. Vectors that land in the same bucket are likely to be close together.
4. On a cache lookup, only the vectors already stored in the query's bucket are pulled from Redis and compared via cosine similarity — not the entire cache. The best match above a configurable `threshold` (default `0.92`) is returned as a hit.

This trades a small amount of recall (near-duplicates that land in adjacent buckets can be missed) for a large reduction in comparison cost.

## Redis data model

| Key | Type | Purpose |
|---|---|---|
| `session:{session_key}:bucket:{bitstring}` | Set | Set of `turn:{id}` hash keys that fall into a given LSH bucket for a given session/tool |
| `turn:{id}` | Hash | Stores `query`, `response`, and `vector` (JSON-encoded) for one cached turn |
| `global:turn:counter` | String (counter) | Auto-incrementing ID generator for `turn:{id}` keys |

`session_key` is constructed as `{session_id}:{tool_name}`, so caching is scoped per-session and per-tool — the same query text won't collide across different tools or different user sessions.

Cache writes use a Redis pipeline with `transaction=True` (atomic `HSET` + `SADD`); cache reads use a non-transactional pipeline purely to batch round-trips.

## Files

| File | Responsibility |
|---|---|
| `proxy_gateway.py` | Spawns `server.py`, proxies stdin/stdout, intercepts `tools/call` traffic, orchestrates cache read/write timing |
| `cache_engine.py` | LSH bucketing, cosine similarity, and the Redis-backed `check_cache` / `write_cache` functions |
| `server.py` | The real MCP server exposing the actual tools (unrelated to caching — the proxy is tool-agnostic and works with any MCP server speaking JSON-RPC over stdio) |

## Configuration

Environment variables (via `.env`, loaded through `python-dotenv`):

| Variable | Description |
|---|---|
| `MCP_SERVER_PYTHON_PATH` | Path to the Python interpreter used to run `server.py` (falls back to `<repo>/.venv/bin/python3`, then the proxy's own interpreter) |

Other constants (currently hardcoded, top of `proxy_gateway.py` / `cache_engine.py`):

- `REDIS_URL` — defaults to `redis://localhost:6379`
- `DIMENSIONS` — 384 (must match the embedding model in use)
- `NUM_PLANES` — 8 (controls the number of LSH buckets: `2^NUM_PLANES`)
- `threshold` — 0.92 cosine similarity cutoff for a cache hit (passed to `check_cache`)

## Setup

1. Have a Redis instance reachable at `REDIS_URL`.
2. Install dependencies: `redis`, `numpy`, `redisvl`, `python-dotenv`, plus whatever `server.py` needs.
3. Set `MCP_SERVER_PYTHON_PATH` if the real server runs in a different virtual environment than the proxy.
4. Point your MCP client at `proxy_gateway.py` instead of `server.py` directly — the proxy transparently launches and wraps the real server.

## Notes & limitations

- The embedding model (`sentence-transformers/all-MiniLM-L6-v2`, via `HFTextVectorizer`) runs locally/offline.
- The hyperplanes are generated with a fixed `np.random.seed(42)`, so bucket assignment is stable across restarts as long as the code doesn't change.
- Cache entries are **never evicted or expired** in the current implementation — the cache grows indefinitely (worth pairing with a `TTL`/`EXPIRE` policy or periodic cleanup for long-running deployments).
- Because the query text is extracted heuristically (first matching key from a fixed list, otherwise the whole argument dict), tools with unusual argument shapes may get coarser cache keys than expected.
- A cache hit is a **hard short-circuit**: the real tool is never invoked. This is now gated by the `cache_able` whitelist (see above), but the whitelist only checks *which tool* was called, not *whether the world has changed* since the last identical call — e.g. `upload_local_folder_to_cloud` is cached on folder path alone, so a hit will report the previous upload's results even if new files were added to that folder since. If a cached write tool's correctness depends on external state, consider folding a state signal (e.g. a hash of the folder listing) into what gets embedded, rather than just the raw argument string.
- The proxy trusts `cacheable_tools` from the server unconditionally at startup; if `server.py` is restarted with different tool definitions mid-session, the proxy won't pick up the change until it also restarts.