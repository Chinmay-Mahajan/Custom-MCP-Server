import asyncio
import json
import sys
import inspect
import aiohttp
import os
import time
from pinecone import Pinecone
from dotenv import load_dotenv
from pinecone import AsyncPinecone
import docker
import tempfile
import shutil
import base64
import uuid
from celery_tasks import run_and_heal, task_store

load_dotenv()
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY") # getting the api key from the env file 
if not PINECONE_API_KEY:
    raise ValueError("Error: PINECONE_API_KEY is missing from environment variables.")
ASSISTANT_NAME = "my-notes" # name of the index made in pinecone. 
# pc = Pinecone(api_key=PINECONE_API_KEY) # initialises communication to pinecone.
pc = AsyncPinecone(api_key=PINECONE_API_KEY)
print("MCP Server: Synchronizing with Pinecone cloud setup...", file=sys.stderr) # we must print the text to the stderr stream not the usual stdout stream
# As the MCP server continously operates on the stdin / stdout streams and if we pass a direct plain text sentence into that stream , the json parser will fail to parse it and cause the entire script to fail.

# Define a stable root directory on your Mac host for the sandbox system
SANDBOX_ROOT = os.path.expanduser("~/.mcp_sandbox_cache")
PKG_DIR = os.path.join(SANDBOX_ROOT, "site-packages")
WORKSPACE_DIR = os.path.join(SANDBOX_ROOT, "workspace")

# Ensure these directories exist on your Mac right when the server boots
os.makedirs(PKG_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)

try:

    active_assistants = pc.assistants.list() # returns the list of all assistants currently running in pinecone
    existing_names = [a.name for a in active_assistants]
    if ASSISTANT_NAME not in existing_names:
        print(f"MCP Server: '{ASSISTANT_NAME}' not found. Spawning new cloud instance...", file=sys.stderr)
        # In v9.x, the method is .create() with 'name=' instead of 'assistant_name=' ---> was stuck here 
        pc.assistants.create(
            name=ASSISTANT_NAME,
            instructions="You are a precise search engine. Extract factual information accurately."
        )
        time.sleep(5)  # used because making new resource in the cloud provider is asynchronous , meaning when i instruct pinecone to make a new database (index) it starts executing it and gives back control to my python loop . Even though the index may not have been made the python script can move forward with uploading files to cloud , or querying the cloud knowlegde base.
    else:
        print(f"MCP Server: Found existing cloud assistant instance '{ASSISTANT_NAME}'.", file=sys.stderr)
    print("MCP Server: Cloud RAG engine connected and synchronized successfully!", file=sys.stderr)

except Exception as init_err:
    print(f"CRITICAL INITIALIZATION ERROR: {str(init_err)}", file=sys.stderr)



class MCPServer:
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self.tools_registry = {} # dict to store corutine objects of the tools.
        self.tools_blueprints = [] # storing the schemea of the tools 

    def tool(self):
        """A python decorator to dynamically register tools with their schemas with the mcp sever registry and blueprint."""
        def decorator(func):
            name = func.__name__
            description = func.__doc__ or "No description provided."
            
            # Auto-generate inputSchema via Python Type Hints
            sig = inspect.signature(func) # used to read the structure of a function 
            properties = {}
            required = []
            
            for param_name, param in sig.parameters.items():
                # param contains meta-data regarding the param_name parameter
                py_type = param.annotation # extract the python type of parameter using type hints 
                json_type = "string" # a safe option , incase we dont have py_type in any of the below
                # we need to convert the json_type to an apt json compatible type . 
                # since json is a universal commnnunication lang used in alot of lang. we need to maintain strict json formatting rules. 
                # And possibly because the LLM was trained on a lot of json with those standard json_types used , it might get confused or start to hallucinate if we provide our python types to it directly . but probably smarter models would figure out the intent 
                if py_type in (int, float):
                    json_type = "number"
                elif py_type == bool:
                    json_type = "boolean"
                elif py_type == dict:
                    json_type = "object"
                elif py_type == list:
                    json_type = "array"
                
                properties[param_name] = {"type": json_type}
                if param.default == inspect.Parameter.empty:
                    required.append(param_name)
                # finds which parameters are required (ie which dont have a default value)

            blueprint = {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
            
            self.tools_registry[name] = func
            self.tools_blueprints.append(blueprint)
            return func
        return decorator

# Instantiate your core server object
server = MCPServer(name="my-scratch-server", version="1.0.0")



@server.tool()
async def calculate_area(width: int, height: int):
    """Calculates the area of a rectangle."""
    return width * height 

@server.tool()
async def greet_user(name: str, formal: bool):
    """Greets a user given their name and preference."""
    if formal:
        return f"Good day, Honorable {name}."
    return f"Hey, what's up {name}!"

@server.tool()
async def optimize_search_query(raw_user_prompt: str):
    """
    Takes a messy, conversational user prompt and extracts the core 
    optimized keywords, synonyms, and technical terms required to execute 
    a highly accurate database vector search.
    """
    
    # when the llm needs clean optimised queries , it will call this tool which asks it to clean the query . So it does and we get a clean query.
    
    instructions = (
        f"You are a RAG Query Optimizer. Analyze the following conversational user prompt:\n"
        f"\"{raw_user_prompt}\"\n\n"
        f"Strip out conversational fluff (like 'please find', 'can you look up'). "
        f"Extract the core technical keywords, add relevant industry synonyms, "
        f"and output ONLY the optimized search string. Do not include introductory text."
    )
    
    return instructions


@server.tool()
async def upload_local_folder_to_cloud(folder_path: str):
    """
    Scans a local directory on users machine and securely uploads all PDFs, TXT, 
    and Markdown files to Pinecone's cloud RAG index. 
    Use this when the user says: 'Sync my documents folder' or 'Upload new files'.
    """
    # Ensure folder exists locally
    if not os.path.exists(folder_path):
        return f"Error: Local path '{folder_path}' could not be located on this machine."

    uploaded_files = []
    errors = []

    # Read the directory contents
    for filename in os.listdir(folder_path):
        if filename.lower().endswith((".pdf", ".txt", ".md", ".json")):
            file_path = os.path.join(folder_path, filename)
            
            # Printing to stderr so we can see the terminal logs real-time
            print(f"MCP Ingestion: Transferring {filename} to Pinecone cloud...", file=sys.stderr)
            
            try:
                # Tell Pinecone's cloud infrastructure to ingest, chunk, and index the file
                # now the pinecone database op is async .
                response = await pc.assistants.upload_file(
                assistant_name=ASSISTANT_NAME,  # v9.x takes the name string directly
                file_path=file_path,
                metadata={"uploaded_via": "mcp-server-tool", "local_source": folder_path}
)
                uploaded_files.append(f"{filename} (ID: {response.id})")
            except Exception as file_error:
                errors.append(f"Failed to upload {filename}: {str(file_error)}")

    # Construct status message back to Claude
    status_report = []
    if uploaded_files:
        status_report.append(f"Successfully processed and indexed {len(uploaded_files)} files:\n" + "\n".join(f"- {f}" for f in uploaded_files))
    if errors:
        status_report.append(f"Encountered {len(errors)} errors during transfer:\n" + "\n".join(f"- {e}" for e in errors))
        
    if not uploaded_files and not errors:
        return "Scan complete. No supported text or PDF files found in that directory."

    return "\n\n".join(status_report)


@server.tool()
async def query_cloud_knowledge_base(query: str):
    """
    Queries our remote cloud document store to extract contextual references.
    Use this whenever the user asks questions about uploaded manuals, documents, or data.
    """
    try:
        # Print a trace statement to stderr so you can see it execution in your Mac terminal
        print(f"RAG Engine: Fetching remote cloud context for query: '{query}'", file=sys.stderr)
        #Ask Pinecone to retrieve relevant text snippets matching the text query.
        # top_k=4 tells it to bring back the 4 best matching document blocks.
        response = await pc.assistants.context(
        assistant_name=ASSISTANT_NAME,
        query=query,
        top_k=4,
        )
        # Extract and format the source citations and text blocks
        context_snippets = []
        for snippet in response.snippets:
            # Safely grab file reference names 
            file_name = snippet.reference.file.get("name", "Unknown Source") if snippet.reference else "Unknown Source"
            content_text = snippet.content
            context_snippets.append(f"--- SOURCE DOCUMENT: {file_name} ---\n{content_text}\n")
        #check if no relevant documents match
        if not context_snippets:
            return "Search complete. No matching references found in the cloud repository."
        #Hand the pure document facts over to Claude
        formatted_payload = "Extracted Documentation Context:\n\n" + "\n".join(context_snippets)
        return formatted_payload
    except Exception as e:
        print(f"RAG Error: {str(e)}", file=sys.stderr)
        return f"Failed to retrieve documentation context from cloud index: {str(e)}"

#  Changes made to the sandbox tools  
# added a tool called install_libs_sandbox to tackle a compromise I was facing. If i free-d some constraints on the sandbox container then I would sacrifice on security.
# protection against IO hogging tasks wasnt their. 
# also if we didnt have changed the installed lib would have been wiped out after the tool execution was finsihed.

# so added install_libs_sandbox tool so that LLM (client) can pass needed lib names and we gave pip install them into this container. 
# Also I have mounted a directory on my mac within which the python lib are installed. 

# in the code_executor tool I just tell python to read lib from that permanant dir on my mac. So I dont need to download the libs again and again 

@server.tool()
async def install_libs_sandbox(libraries: list):
    """
    Installs one or more third-party Python packages (e.g., ['numpy', 'requests']) 
    into the secure sandbox environment. 
    Use this tool BEFORE executing code if the user's script requires external libraries.
    """
    if not libraries:
        return "No libraries specified for installation."

    print(f"Sandbox Infra: Launching Installer for: {libraries}", file=sys.stderr)
    
    def run_installer():
        client = docker.from_env()
        libs_string = " ".join(libraries)
        try:
            # Phase 1: High resource allowance, network active, but isolated execution command
            # It maps the host package directory as Read-Write (rw)
            client.containers.run(
                image="python:3.11-alpine",
                #tell pip to install directly into /cache
                command=[
                    "sh", "-c", 
                    f"python3 -m ensurepip --upgrade && pip install --no-cache-dir --target=/cache {libs_string}"
                ],
                volumes={
                    # Mount host cache directory to /cache inside the container , this makes sure the packages are installed in the cache dir . allowing persistant storage of installed python libs
                    PKG_DIR: {"bind": "/cache", "mode": "rw"}
                },
                network_disabled=False, 
                mem_limit="512m",       
                nano_cpus=1000000000,   
                remove=True             
            )
            return f"SUCCESS: Successfully installed and cached libraries: {libraries}"
        except Exception as e:
            return f"INSTALLATION FAILED: {str(e)}"

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, run_installer)
    return result



@server.tool()
async def execute_code_sandbox(code: str):
    """
    Executes raw Python code inside a completely isolated, resource-constrained 
    Docker container. Features a strict 30-second timeout and 512MB RAM limit.
    Network is COMPLETELY DISABLED. Pre-installed libraries can be imported natively.
    """
    # setup a  isolated workspace inside a temporary directory on my Mac
    # We execute this inside an async thread pool to keep the main I/O channel completely free.
    loop = asyncio.get_running_loop()
    # temp_dir = await loop.run_in_executor(None, tempfile.mkdtemp) # make or use a worker thread to make a temp dir
    # script_path = os.path.join(temp_dir, "sandbox_script.py") # path to the sandbox python file
    # removed the use of temp dir. instead using a permanat dir on my mac.
    script_path = os.path.join(WORKSPACE_DIR, "sandbox_script.py")
    
    def write_script_file():
        with open(script_path, "w", encoding="utf-8") as f: # opening the sand_box file in write mode and writing the python code in it , code being a str 
            f.write(code)
    
    await loop.run_in_executor(None, write_script_file) # again assign a worker thread to exceute this function , we do this because the write_script_file() func is a blocking function . ie. it is synchronous.
    print(f"Sandbox Infra: Script written to temporary volume space: {script_path}", file=sys.stderr)

    # Internal worker function that runs on a separate thread to interact with the Docker Engine
    def run_container():
        client = docker.from_env()
        container = None
        try:
            # Spawn the strictly resource-constrained container
            container = client.containers.run(
                image="python:3.11-alpine",
                # Execute the script directly and immediately exit when done
                command=["python", "/workspace/sandbox_script.py"],
                volumes={
                   PKG_DIR: {"bind": "/cache", "mode": "ro"}, # Read-only because we do not trust the llm (the llm should not be always trusted.) because docker doesnt copy our temp file into the container. It instead exposes the temp file through the container. It is like assigning a var b = a . b isnt a copy of a , it IS a any chnage in b results in change in a. If the mount were writable, code running inside the container could modify, delete, or create files in the mounted host directory. hence we restrict the llm to only reading code from this file and not writing code into this file.
                    WORKSPACE_DIR: {"bind": "/workspace", "mode": "ro"}
                },
                environment={
                    "PYTHONPATH": "/cache" # this instructs python to use the cache dir to look for installed packages.
                },
                detach=True, # with detach = true , the python script for the server and the one in the sandbox run parallelly . The container object gets returned immediately and we can monitor it as it is running it's python script in the sandbox. without detach this server.py would have been blocked till the contaiiner has finished running.
                network_disabled=True,      #no outbound internet access allowed 
                mem_limit="512m",           # strict protection metric against memory leaks , allocating 128mb of RAM # changed to 512 mb to allow pip commands
                nano_cpus=100000000,        # cap maximum execution speed at 30% of a single CPU core 
                # actually docker measures 1 CPU = 1,000,000,000 nano CPUs so we are using 0.1% of cpu's compute for this sandbox
                pids_limit=10       
            )
            
            # Enforce the absolute 30 second execution deadline wall
            # This waits for the container status to shift to finished
            # increased form initial 3 seconds because importing numpy / pandas can itself take longer that 10-15 seconds. the exact 30 second value was a guess.
            result = container.wait(timeout=30) 
            exit_code = result.get("StatusCode", 0)
            
            # Fetch the compiled execution streams
            logs = container.logs(stdout=True, stderr=True).decode("utf-8")
            return exit_code, logs
            
        except docker.errors.ContainerError as exc:
            return -1, f"Execution Container Error: {str(exc)}"
        except Exception as exc:
            # If a timeout exception triggers, handle the recovery and cleanup steps immediately
            if "timeout" in str(exc).lower() or "read timeout" in str(exc).lower():
                if container:
                    try:
                        container.kill() # Force kill the runaway process thread instantly
                    except Exception:
                        pass
                return 124, "TIMEOUT ERROR: Execution exceeded the strict 3.0-second safety deadline."
            return -1, f"Sandbox Infrastructure Runtime Failure: {str(exc)}"
            
        finally:
            # wipe every container trace out of memory space
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            try:
                os.remove(script_path)
            except Exception:
                pass   

            # wipe the host machine's temporary file directory cleanly
            

    # Run our secure container lifecycle routine seamlessly off the main thread
    try:
        print("Sandbox Infra: Allocating resource cgroups and booting container...", file=sys.stderr)
        exit_code, output_logs = await loop.run_in_executor(None, run_container)
        # we run the run_container function in a worker thread but we still need detach=True to safely run code. 
        
        # 4. Construct the structural report back to Claude over the wire
        status_label = "SUCCESS" if exit_code == 0 else "FAILED / RUNTIME EXCEPTION"
        if exit_code == 124:
            status_label = "TIMEOUT BOUNDARY BREACHED"

        report = (
            f"--- SANDBOX EXECUTION REPORT ---\n"
            f"Process Status: {status_label}\n"
            f"System Exit Code: {exit_code}\n"
            f"--- STANDARD OUTPUT / ERROR STREAMS ---\n"
            f"{output_logs if output_logs.strip() else '[No output returned]'}\n"
        )
        return report

    except Exception as server_error:
        print(f"CRITICAL SANDBOX FAULT: {str(server_error)}", file=sys.stderr)
        return f"Infrastructure Failure: Could not successfully interface with local Docker daemon: {str(server_error)}"

@server.tool()
async def generate_and_pack_crust(files: list, archive_name: str):
    """
    Generates text or markdown files completely within an isolated, non-persistent container, 
    compiles them into a custom .crust archive, and streams the finished binary stream 
    directly back to the chat interface for immediate download.
    
    This tool DOES NOT write uncompressed data or modify paths on your Mac host machine.
    
    Args:
        files (list): A list of dicts. Each dict MUST have 'path' (relative only, e.g., 'main.py') 
                      and 'content' (the raw text payload).
        archive_name (str): Clean target name for the output archive (e.g., 'workspace.crust').
    """
    if not files:
        return "Error: No files provided to package."
        
    if not archive_name.endswith(".crust"):
        archive_name += ".crust"

    loop = asyncio.get_running_loop()

    def run_isolated_pipeline():
        client = docker.from_env()
        container = None
        try:
            #Boot up custom image.
            # It runs with a purely internal container filesystem.
            container = client.containers.run(
                image="crust-sandbox:latest",
                command="sleep 300", # Boot it as a short-lived daemon so we can inject setups
                detach=True,
                network_disabled=True,
                mem_limit="512m",
                pids_limit=15
            )

            # Establish isolated workspaces INSIDE the container's internal filesystem
            container.exec_run("mkdir -p /tmp/staging_zone /tmp/output_zone")

            #Write each text file directly inside the container's boundary
            for file_item in files:
                rel_path = file_item.get("path", "").lstrip("/")
                content = file_item.get("content", "")
                if not rel_path:
                    continue
                
                # Double-escape single quotes for safe shell payload passing
                safe_content = content.replace("'", "'\\''")
                
                # Make sure child directories exist inside the container
                container.exec_run(f"mkdir -p /tmp/staging_zone/{os.path.dirname(rel_path)}")
                
                # Drop the text contents directly to the virtual container storage
                container.exec_run(
                    cmd=["sh", "-c", f"cat << 'EOF' > /tmp/staging_zone/{rel_path}\n{safe_content}\nEOF"]
                )

            # Trigger custom embedded Rust binary inside the container room
            pack_cmd = f"crust pack /tmp/staging_zone /tmp/output_zone/{archive_name}"
            exec_res = container.exec_run(cmd=["sh", "-c", pack_cmd])
            
            if exec_res.exit_code != 0:
                return f"CRUST_COMPILER_ERROR: {exec_res.output.decode('utf-8')}"

            # Extract the finished .crust file bytes directly out of the container's RAM/virtual disk
            bits_stream_res = container.exec_run(cmd=["cat", f"/tmp/output_zone/{archive_name}"])
            if bits_stream_res.exit_code != 0:
                return "Error: Unable to stream bytes back from output sector."
                
            raw_binary_bytes = bits_stream_res.output

            #Encode the binary package directly into standard Base64 text
            b64_string = base64.b64encode(raw_binary_bytes).decode("utf-8")
            return ("SUCCESS", b64_string)

        except Exception as e:
            return ("FAILED", str(e))
        finally:
            # Completely terminate and vaporize the container footprint instantly
            if container:
                try:
                    container.kill()
                    container.remove(force=True)
                except Exception:
                    pass

    print("Crust Engine: Booting zero-host packaging matrix...", file=sys.stderr)
    result = await loop.run_in_executor(None, run_isolated_pipeline)

    if isinstance(result, tuple) and result[0] == "SUCCESS":
        b64_payload = result[1]
        return (
            f"--- CRUST CONTAINER PACKAGING COMPLETE ---\n"
            f"Archive Name: {archive_name}\n"
            f"Payload Security Status: Host Isolated (No Local File Writes)\n"
            f"Encoding Format: Base64 Stream\n"
            f"========================================\n"
            f"{b64_payload}\n"
            f"----------------------------------------"
        )
    else:
        error_details = result[1] if isinstance(result, tuple) else result
        return f"--- CRUST PACKAGING REPORT ---\nStatus: FAILED\nReason: {error_details}"

@server.tool()
async def queue_coding_task(task_name: str, code: str):
    """
    Queues Python code to run in the sandbox as a background task with
    automatic error-healing retries (a cheaper background LLM attempts up
    to 3 fixes on its own). Returns immediately with a task_id — does NOT
    block. Use list_active_tasks to check progress and get_task_result to
    retrieve the final code or failure details once it's done.
    """
    task_id = str(uuid.uuid4())[:8]
    task_store.set(f"task:{task_id}", json.dumps({"name": task_name, "status": "QUEUED"}))
    task_store.sadd("all_task_ids", task_id)
    run_and_heal.delay(task_id, task_name, code)
    return f"Queued '{task_name}' as task {task_id}. Check back with list_active_tasks."


@server.tool()
async def list_active_tasks():
    """
    Lists every background coding task and its current status
    (QUEUED, SUCCESS, or STUCK). Call this to check on tasks queued
    earlier without pulling full code or errors into context.
    """
    task_ids = task_store.smembers("all_task_ids")
    if not task_ids:
        return "No tasks queued yet."
    lines = []
    for tid in task_ids:
        raw = task_store.get(f"task:{tid}")
        if raw:
            d = json.loads(raw)
            lines.append(f"{tid} — {d['name']}: {d['status']}")
    return "\n".join(lines)


@server.tool()
async def get_task_result(task_id: str):
    """
    Retrieves full details for one background task: the final working code
    if it succeeded, or the complete failure history across all auto-retry
    attempts if it's still stuck.
    """
    raw = task_store.get(f"task:{task_id}")
    if not raw:
        return f"No task found with id '{task_id}'."
    d = json.loads(raw)
    if d["status"] == "QUEUED":
        return f"Task '{d['name']}' is still running."
    if d["status"] == "SUCCESS":
        # return f"Status: SUCCESS\nSummary: {d['summary']}\n\n--- FINAL WORKING CODE ---\n{d['final_code']}"
        return (
        f"Status: SUCCESS\nSummary: {d['summary']}\n\n"
        f"--- STDOUT ---\n{d['final_output']}\n\n"
        f"--- FINAL WORKING CODE ---\n{d['final_code']}"
    )
    history = "\n\n".join(
        f"--- Attempt {a['attempt']} ---\nCode:\n{a['code']}\n\nError:\n{a['output']}"
        for a in d["attempts"]
    )
    return f"Status: STUCK\nSummary: {d['summary']}\n\n{history}"


@server.tool()
async def clear_task_history(only_finished: bool = True):
    """
    Clears background coding task records. By default only removes
    finished tasks (SUCCESS/STUCK), leaving anything still QUEUED intact.
    Pass only_finished=False to wipe everything, including in-progress
    or orphaned entries.
    """
    task_ids = task_store.smembers("all_task_ids")
    cleared = 0
    for tid in task_ids:
        raw = task_store.get(f"task:{tid}")
        if not raw:
            continue
        status = json.loads(raw).get("status")
        if only_finished and status == "QUEUED":
            continue
        task_store.delete(f"task:{tid}")
        task_store.srem("all_task_ids", tid)
        cleared += 1
    return f"Cleared {cleared} task record(s)."


stdout_lock = asyncio.Lock() # ensures that only one task can write to the stdout stream at a time
async_writer = None


@server.tool()
async def list_indexes_in_database():
    '''
    Lists all files uploaded to the configured Pinecone Assistant. Use this before calling upload_files to prevent duplicates
    '''
    try:
        files = pc.assistants.list_files(assistant_name=ASSISTANT_NAME)
        data = ""
        async for  file in files:
            data += f"{file.name}\n"
        return data    

    except Exception as e:
        print(f"Could not call list files method: {e}", file=sys.stderr)
        return f"Error: {e}"

@server.tool()
async def look_inside_a_folder(folder_path : str):
    '''
    Lists all files present directly inside a specified local directory path, excluding subdirectories.
    
    CRITICAL USAGE RULE: Call this tool immediately BEFORE performing any file upload or database 
    ingestion tasks requested by the user. This ensures you verify the existence, exact names, 
    and availability of the local files before attempting to process them.

    After calling this tool . immediately call the list_indexes_in_database tool and verify that any file in the user directory is also present in the 
    vector database . If yes then DO NOT execute the ingestion tool . And politely warn the user.
    
    Args:
        folder_path (str): The absolute or relative local system path to the directory.
        
    Returns:
        str: A newline-separated string containing the names of files found, or an error message string.
    '''
   
    try :
        data = ""
        files = os.listdir(folder_path) 
        for file in files :
            file_path = os.path.join(folder_path , file) 
            if not os.path.isdir(file_path):
                data += f"{file}\n"
        return data        
    except Exception as e :
        return f"Error occured {e}"    



async def send_response(response):
    """Writes a JSON-RPC response back to stdout asynchronously without blocking."""
    global async_writer
    output = (json.dumps(response) + "\n").encode("utf-8")
    async with stdout_lock:
        if async_writer:
            async_writer.write(output)
            # await drain() yields control back to the event loop, 
            # allowing the OS to clear its buffer without throwing Errno 35!
            await async_writer.drain()
        else:
            # Fallback if initialization hasn't happened yet
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

async def handle_request(request):
    req_id = request.get("id")
    method = request.get("method")
    
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools":{"listChanged": True}},
                "serverInfo": {"name": server.name, "version": server.version}
            }
        }
    
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": server.tools_blueprints}
        }
    
    elif method == "notifications/initialized":
        # The client is just letting us know the handshake is complete.
        # It's a notification, so we return None (no response expected).
        return None

    elif method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name in server.tools_registry:
            func = server.tools_registry[tool_name]
            try:
                # unpacking the dict args straight into the function kwargs
                result_data = await func(**arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(result_data)}]
                    }
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"Execution error: {str(e)}"}
                }
        
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Tool {tool_name} not found"}
        }
    
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method {method} not found"}
    }

async def process_request(request):
    try:
        response = await handle_request(request)
        if response:
            await send_response(response)
    except Exception as e:
        error_response = {
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
        }
        await send_response(error_response)



async def main():
    global async_writer
    loop = asyncio.get_running_loop()
    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, 
        sys.stdout
    )
    async_writer = asyncio.StreamWriter(w_transport, w_protocol, None, loop)
    # boilerplate code to construct a async writer 
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    print("Server parsing loop running smoothly with Async I/O...", file=sys.stderr)
    while True:
        line = await reader.readline()
        if not line:
            break  
        decoded_line = line.decode().strip()
        if not decoded_line:
            continue
        try:
            request = json.loads(decoded_line)
            asyncio.create_task(process_request(request))
        except Exception as e:
            print("exception:", e, file=sys.stderr)
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
            }
            await send_response(error_response)

if __name__ == "__main__":
    print("Server started, listening on stdin...", file=sys.stderr)
    asyncio.run(main())