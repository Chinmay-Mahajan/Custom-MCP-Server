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
            command=["python", "/workspace/script.py"],
            volumes={
                PKG_DIR: {"bind": "/cache", "mode": "ro"},
                # WORKSPACE_DIR: {"bind": "/workspace", "mode": "ro"}
                script_path: {"bind": "/workspace/script.py", "mode": "ro"}
            },
            environment={"PYTHONPATH": "/cache"},
            detach=True,
            network_disabled=True,
            mem_limit="512m",
            nano_cpus=100000000,
            pids_limit=10, 
            user="1000:1000", # running these containers as a non root user , 
            cap_drop=["ALL"] # dropping all the linux capabilites 
            # If the LLM queues a python script that can exploit kernel level vulnerabilities then the container can be escaped and the malicious code can interact with the other docker containers that are running on the same linux VM kernel. 
            # stuff like our cache could be compromised . 

            # having docker run containers on linux VM saves my mac from a container escape but within the VM the other containers can be stopped and redis's stored data (which is on the ram ) can be read

        )

        # SECURITY RISK --> so previously I was mounting the WORKSPACE dir to each container , so if a python code like os.listdir(workspace) and can list all the files and also could also read them.
        # This destroys the container isolation . 
        # The fix i did. --> I basically now mount only the script and not the full dir .
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
