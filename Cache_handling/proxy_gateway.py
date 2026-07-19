import os
import sys
import json
import asyncio
import redis.asyncio as aioredis
from redisvl.utils.vectorize import HFTextVectorizer
from dotenv import load_dotenv

load_dotenv() 


REDIS_URL = "redis://localhost:6379"
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) 
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR) 
DEBUG_LOG_PATH = os.path.join(BASE_DIR , "cache_debug.log")
SERVER_SCRIPT_PATH = os.path.join(BASE_DIR, "..", "server.py")
VENV_PYTHON = os.getenv("MCP_SERVER_PYTHON_PATH")
if not VENV_PYTHON:
    VENV_PYTHON = os.path.abspath(os.path.join(BASE_DIR, "..", ".venv", "bin", "python3"))
    if not os.path.exists(VENV_PYTHON):
        VENV_PYTHON = sys.executable


CACHED_TOOLS_WHITELIST = set()
from cache_engine import check_cache, write_cache


# Local vector processing via a small, offline sentence-transformers configuration
vectorizer = HFTextVectorizer(model="sentence-transformers/all-MiniLM-L6-v2")
pending_cache_writes = {}

def log_proxy_action(text: str):
    """Appends clear structural audit logs directly to a dedicated local file."""
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

async def handle_client_to_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, redis_client: aioredis.Redis):
    """Monitors incoming JSON-RPC traffic arriving from the Agent (claude or any other llm) interface."""
    while True:
        line = await reader.readline()
        if not line:
            break

        try:
            raw_text = line.decode('utf-8').strip()
            log_proxy_action(f"RAW INBOUND PACKET: {raw_text}")
            
            packet = json.loads(raw_text)
            
            if packet.get("method") == "tools/call":
                params = packet.get("params", {})
                tool_name = params.get("name")
                args = params.get("arguments", {})
                
                if tool_name not in CACHED_TOOLS_WHITELIST: # skip cache layer
                    log_proxy_action(f"BYPASS] '{tool_name}' is not cacheable. Routing straight to server.py.")
                    writer.write(line)
                    await writer.drain()
                    continue  


                log_proxy_action(f"Tool intercepted: '{tool_name}' | Args Keys: {list(args.keys())}")
                query = None
                for key in ["query", "raw_user_prompt", "code", "text", "prompt", "folder_path"]:
                    if key in args:
                        query = str(args[key])
                        break
                
                if not query and args:
                    query = json.dumps(args, sort_keys=True)
                
                session_id = args.get("session_id", "default_session")

                if query:
                    query_vector = vectorizer.embed(query)
                    cache_key = f"{session_id}:{tool_name}"
                    hit = await check_cache(redis_client, cache_key, query_vector)
                    
                    if hit:
                        log_proxy_action(f"CACHE HIT - Short-circuiting tool '{tool_name}' for input string: '{query}'")
                        # since it is a cache hit we write to the stdout stream the cached response 
                        fake_res = {
                            "jsonrpc": "2.0",
                            "result": {
                                "content": [{"type": "text", "text": f"[Proxy Cache Hit]\n\n{hit}"}]
                            },
                            "id": packet["id"]
                        }
                        sys.stdout.write(json.dumps(fake_res) + "\n")
                        sys.stdout.flush()
                        continue  # Drops the execution line from traveling to server.py!

                    log_proxy_action(f"CACHE MISS. Routing ID {packet['id']} to server.py for: '{query}'")
                    pending_cache_writes[packet["id"]] = {
                        "session_key": cache_key,
                        "query_text": query,
                        "query_vector": query_vector
                    }

        except Exception as e:
            log_proxy_action(f"PROXY INBOUND EXCEPTION: {str(e)}")

        writer.write(line)
        await writer.drain()

async def handle_server_to_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, redis_client: aioredis.Redis):
    """Captures computational output packets returning from the real server.py process."""
    global CACHED_TOOLS_WHITELIST
    while True:
        line = await reader.readline()
        if not line:
            break
        line_to_send = line

        try:
            raw_text = line.decode('utf-8').strip()
            packet = json.loads(raw_text)
            packet_id = packet.get("id")

            if "result" in packet and "serverInfo" in packet["result"]:
                server_info = packet["result"]["serverInfo"]
                if "meta" in server_info and "cacheable_tools" in server_info["meta"]:
                    CACHED_TOOLS_WHITELIST = set(server_info["meta"]["cacheable_tools"])
                    log_proxy_action(f"[PROXY INITIALIZED] Successfully Synchronized Whitelist: {list(CACHED_TOOLS_WHITELIST)}")
                    del packet["result"]["serverInfo"]["meta"]
                    line_to_send = (json.dumps(packet) + "\n").encode('utf-8')


            if packet_id in pending_cache_writes:
                cache_meta = pending_cache_writes.pop(packet_id)
                results = packet.get("result", {}).get("content", [])
                is_error_payload = packet.get("result", {}).get("isError", False)
                if results and results[0].get("type") == "text" and not is_error_payload:
                    llm_response = results[0]["text"]
                    

                    asyncio.create_task(
                        write_cache(
                            redis_client,
                            session_key=cache_meta["session_key"],
                            query_text=cache_meta["query_text"],
                            query_vector=cache_meta["query_vector"],
                            response_text=llm_response
                        )
                    )
                    log_proxy_action(f"CACHE WRITE COMPLETED.")
                    
        except Exception as e:
            log_proxy_action(f"PROXY OUTBOUND EXCEPTION: {str(e)}")
            line_to_send = line # Safe fallback

        writer.write(line_to_send)
        await writer.drain()

async def clear_semantic_cache(redis_client) -> str:
    """Scans and clears all proxy semantic keys, sets, and counters from Redis."""
    # locate all specific keys used by the caching engine
    turn_keys = await redis_client.keys("turn:*")
    session_keys = await redis_client.keys("session:*")
    counter_keys = await redis_client.keys("global:turn:counter")
    
    all_target_keys = turn_keys + session_keys + counter_keys
    
    if not all_target_keys:
        return "Cache is already empty. No indices found to wipe."
    
    # delete the matched tracking blocks using a pipeline
    async with redis_client.pipeline(transaction=True) as pipe:
        for key in all_target_keys:
            pipe.delete(key)
        await pipe.execute()
        
    return f"SUCCESS: Successfully purged {len(all_target_keys)} semantic cache keys from Redis memory."



async def main():
    log_proxy_action("\nProxy Gateway starting up...")
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    
    # we start up the server.py in a child process (since we want the server to be responsive) , we have to specify the python env that we will use (that has all our dependencies) 
    # and the actual path to the python file 
    # The server and the proxy communicate over a private memeory pipeline completely sep than proxy and client 
    server_process = await asyncio.create_subprocess_exec(
        VENV_PYTHON, 
        "-u", 
        SERVER_SCRIPT_PATH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE
    )

    loop = asyncio.get_running_loop()
    client_reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(client_reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    w_transport, w_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(), sys.stdout
    )
    client_writer = asyncio.StreamWriter(w_transport, w_protocol, client_reader, loop)

    await asyncio.gather(
        handle_client_to_server(client_reader, server_process.stdin, redis_client),
        handle_server_to_client(server_process.stdout, client_writer, redis_client)
    )

if __name__ == "__main__":
    asyncio.run(main())