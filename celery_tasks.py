from celery import Celery
import redis
import json
from google import genai
from dotenv import load_dotenv
from sandbox_runner import run_code_in_sandbox
import re 

# Adding this feature to allow user (me) to queue multiple code execution tasks together in a non blocking fashion . 

load_dotenv()

celery_app = Celery(
    "celery_tasks",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0"
)

# celery makes a seperate manager process with a pre-defined number of child - processes to execute the queued tasks.
# since the child processes (and processes in general) each have their own memory (isolated from other process) we use redis which is a key value store on my machines RAM (hence it is really fast)
# we send the data from the child process via redis . The data goes back to who-ever called .delay() (mcp server) . the manager process has nothing to do with the data
# this works because Redis is a separate, shared process both the child
# worker and the MCP server can reach independently — they never talk
# to each other directly.

task_store = redis.Redis(host="localhost", port=6379, db=2, decode_responses=True)

MAX_RETRIES = 3


def strip_code_fences(text: str) -> str:
    """Removes markdown code fences if the model added them despite instructions.
    Handles ```python ... ```, ``` ... ```, and any leading/trailing whitespace."""
    text = text.strip()
    fence_pattern = r"^```[a-zA-Z]*\n?(.*?)\n?```$"
    match = re.match(fence_pattern, text, re.DOTALL)

    if match:
        return match.group(1).strip()

    return text

def ask_fixer_llm(code: str, error_output: str) -> str:
    client = genai.Client()
    
    prompt = (
        f"This Python script failed when run.\n\n"
        f"--- CODE ---\n{code}\n\n"
        f"--- ERROR OUTPUT ---\n{error_output}\n\n"
        f"Return ONLY the corrected, complete Python script. "
        f"No explanation, no markdown fences, just the raw code."
    )
    
    # using gemini-2.5-flash which is highly optimal for coding tasks and includes a free tier
    # ONE BUG THOUGH GOTTA HAVE TO ADD A SAFETY NET TO REMOVE THOSE ``` PYTHON` TAGS using regex..

    #fixed it using regex filter 
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )
    
    return strip_code_fences(response.text.strip())


def _same_error(output_a: str, output_b: str) -> bool:
    def last_line(s):
        lines = [l for l in s.strip().splitlines() if l.strip()]
        return lines[-1] if lines else ""
    return last_line(output_a) == last_line(output_b)


def _save(task_id, name, status, final_code, attempts , final_output = None):
    record = {
        "name": name,
        "status": status,
        "final_code": final_code,
        "attempt_count": len(attempts),
        "attempts": attempts if status == "STUCK" else [],
        "final_output":final_output,
        "summary": (
            "Passed on first try." if status == "SUCCESS" and len(attempts) == 1
            else f"Fixed after {len(attempts) - 1} auto-retry attempt(s)." if status == "SUCCESS"
            else f"Still failing after {len(attempts)} attempt(s)."
        )
    }
    task_store.set(f"task:{task_id}", json.dumps(record)) # store the record with the task_id key
    task_store.sadd("all_task_ids", task_id) # save the ids into a set with the key "all_task_ids"


@celery_app.task
def run_and_heal(task_id: str, task_name: str, code: str):
    attempts = []
    current_code = code

    for attempt_num in range(1, MAX_RETRIES + 1):
        exit_code, output = run_code_in_sandbox(current_code)
        attempts.append({"attempt": attempt_num, "code": current_code, "output": output})

        if exit_code == 0:
            _save(task_id, task_name, "SUCCESS", current_code, attempts, output)
            return 

        if attempt_num > 1 and _same_error(attempts[-1]["output"], attempts[-2]["output"]):
            break

        if attempt_num < MAX_RETRIES:
            current_code = ask_fixer_llm(current_code, output)

    _save(task_id, task_name, "STUCK", current_code, attempts)