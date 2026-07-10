import docker
import os
import uuid

SANDBOX_ROOT = os.path.expanduser("~/.mcp_sandbox_cache") 
PKG_DIR = os.path.join(SANDBOX_ROOT, "site-packages")
WORKSPACE_DIR = os.path.join(SANDBOX_ROOT, "workspace")
os.makedirs(PKG_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)

# using the same PKG_DIR for all child processes of the manager process 


def run_code_in_sandbox(code: str) -> tuple[int, str]:
    '''writes code to a unique filename each call so parallel runs never collide.'''
    script_name = f"sandbox_{uuid.uuid4().hex[:8]}.py" 
    # by using first 8 characters the prob of getting two same filenames is 1 / 16^8 = 1 / 4.294 Billion. ---> !!! But this is only true for the first two files
    # I didn't know about this though but turns out the prob of getting two same filenames is approx = 1 - exp(-n^2 / 2d) where d is the total number of combinations and n is the number of files 
    # so by that when. n = 77,000 there is a 50% chance of two file having the same filename , but to be honest in this usecase which is just me using claude that number is not going to achievable.
    # Though one way is to increase to all 32 characters (but that will require 128 bits = 32 bytes to store)..(x4 current)
    script_path = os.path.join(WORKSPACE_DIR, script_name)

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    client = docker.from_env()
    container = None
    try:
        container = client.containers.run(
            image="python:3.11-alpine",
            command=["python", f"/workspace/{script_name}"],
            volumes={
                PKG_DIR: {"bind": "/cache", "mode": "ro"},
                WORKSPACE_DIR: {"bind": "/workspace", "mode": "ro"}
            },
            environment={"PYTHONPATH": "/cache"},
            detach=True,
            network_disabled=True,
            mem_limit="512m",
            nano_cpus=100000000,
            pids_limit=10
        )
        result = container.wait(timeout=30)
        exit_code = result.get("StatusCode", 0)
        logs = container.logs(stdout=True, stderr=True).decode("utf-8")
        return exit_code, logs

    except docker.errors.ContainerError as exc:
        return -1, f"Execution Container Error: {str(exc)}"
    except Exception as exc:
        if container:
            try:
                container.kill()
            except Exception:
                pass
        return -1, f"Sandbox Infrastructure Runtime Failure: {str(exc)}"
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass
        try:
            os.remove(script_path)
        except Exception:
            pass
