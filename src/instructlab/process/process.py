# SPDX-License-Identifier: Apache-2.0

# Standard
from datetime import datetime
from typing import Callable
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid

# Third Party
from filelock import FileLock
import psutil

# First Party
from instructlab.configuration import DEFAULTS
from instructlab.defaults import ILAB_PROCESS_MODES

logger = logging.getLogger(__name__)


class ProcessRegistry:
    def __init__(self):
        self.processes = {}

    def add_process(self, local_uuid, pid, children_pids, type, log_file, start_time):
        self.processes[str(local_uuid)] = {
            "pid": pid,
            "children_pids": children_pids,
            "type": type,
            "log_file": log_file,
            "start_time": datetime.strptime(
                start_time, "%Y-%m-%d %H:%M:%S"
            ).isoformat(),
            "done": False,
        }

    def load_entry(self, key, value):
        self.processes[key] = value


def load_registry() -> ProcessRegistry:
    process_registry = ProcessRegistry()
    lock_path = DEFAULTS.PROCESS_REGISTRY_LOCK_FILE
    lock = FileLock(lock_path, timeout=1)
    """Load the process registry from a file, if it exists."""
    # we do not want a persistent registry in memory. This causes issues when in scenarios where you switch registry files (ex, in a unit test, or with multiple users)
    # but the registry with incorrect processes still exists in memory.
    with lock:
        if os.path.exists(DEFAULTS.PROCESS_REGISTRY_FILE):
            with open(DEFAULTS.PROCESS_REGISTRY_FILE, "r") as f:
                data = json.load(f)
                for key, value in data.items():
                    process_registry.load_entry(key=key, value=value)
        else:
            logger.debug("No existing process registry found. Starting fresh.")
    return process_registry


def save_registry(process_registry):
    """Save the current process registry to a file."""
    lock_path = DEFAULTS.PROCESS_REGISTRY_LOCK_FILE
    lock = FileLock(lock_path, timeout=1)
    with lock, open(DEFAULTS.PROCESS_REGISTRY_FILE, "w") as f:
        json.dump(dict(process_registry.processes), f)


class Tee:
    def __init__(self, log_file):
        """
        Initialize a Tee object.

        Args:
            log_file (str): Path to the log file where the output should be written.
        """
        self.log_file = log_file
        self.terminal = sys.stdout
        self.log = log_file  # Line-buffered

    def write(self, message):
        """
        Write the message to both the terminal and the log file.

        Args:
            message (str): The message to write.
        """
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        """
        Ensure all data is written to the terminal and the log file.
        """
        self.terminal.flush()
        self.log.flush()

    def close(self):
        """
        Close the log file.
        """
        if self.log:
            self.log.close()


def format_command(
    target: Callable, extra_imports: list[tuple[str, ...]], **kwargs
) -> str:
    """
    Formats a command given the target and any extra python imports to add

    Args:
        target: Callable
        extra_imports: list[tuple[str, ...]]
    Returns:
        cmd: str
    """
    # Prepare the subprocess command string
    cmd = (
        f"import {target.__module__}; {target.__module__}.{target.__name__}(**{kwargs})"
    )

    # Handle extra imports (if any)
    if extra_imports:
        import_statements = "\n".join(
            [f"from {imp[0]} import {', '.join(imp[1:])}" for imp in extra_imports]
        )
        cmd = f"{import_statements}\n{cmd}"
    return cmd


def start_process(cmd: str, log) -> tuple[int | None, list[int] | None]:
    """
    Starts a subprocess and captures PID and Children PIDs

    Args:
        cmd: str
        log: _FILE

    Returns:
        pid: int
        children_pids: list[int]
    """
    children_pids = []
    p = subprocess.Popen(
        ["python", "-c", cmd],
        universal_newlines=True,
        text=True,
        stdout=log,
        stderr=log,
        start_new_session=True,
        encoding="utf-8",
        bufsize=1,  # Line-buffered for real-time output
    )
    time.sleep(1)
    # we need to get all of the children processes spawned
    # to be safe, we will need to try and kill all of the ones which still exist when the user wants us to
    # however, representing this to the user is difficult. So let's track the parent pid and associate the children with it in the registry
    max_retries = 5
    retry_interval = 0.5  # seconds
    parent = psutil.Process(p.pid)
    for _ in range(max_retries):
        children = parent.children(recursive=True)
        if children:
            for child in children:
                children_pids.append(child.pid)
            break
        time.sleep(retry_interval)
    else:
        logger.debug("No child processes detected. Tracking parent process.")
    # Check if subprocess was successfully started
    if p.poll() is not None:
        logger.warning(f"Process {p.pid} failed to start.")
        return None, None  # Process didn't start
    return p.pid, children_pids


def add_process(
    process_mode: str,
    process_type: str,
    target: Callable,
    extra_imports: list[tuple[str, ...]],
    **kwargs,
):
    """
    Start a detached process using subprocess.Popen, logging its output.

    Args:
        process_mode (str): Mode we are running in, Detached or Attached.
        process_type (str): Type of process, ex: Generation.
        target (func): The target function to kick off in the subprocess or to run in the foreground.
        extra_imports (list[tuple(str...)]): a list of the extra imports to splice into the python subprocess command.

    Returns:
        None
    """
    process_registry = load_registry()
    if target is None:
        return None, None

    local_uuid = uuid.uuid1()
    log_file = None

    log_dir = os.path.join(DEFAULTS.LOGS_DIR, process_type.lower())

    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, f"{process_type.lower()}-{local_uuid}.log")
    pid: int | None = os.getpid()
    children_pids: list[int] | None = []
    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kwargs["log_file"] = log_file
    if process_mode == ILAB_PROCESS_MODES.DETACHED:
        assert isinstance(log_file, str)
        cmd = format_command(target=target, extra_imports=extra_imports, **kwargs)
        # Open the subprocess in the background, redirecting stdout and stderr to the log file
        with open(log_file, "a+") as log:
            pid, children_pids = start_process(cmd=cmd, log=log)
            if pid is None or children_pids is None:
                # process didn't start
                return None
            assert isinstance(pid, int) and isinstance(children_pids, list)
    # Add the process info to the shared registry
    process_registry.add_process(
        local_uuid=local_uuid,
        pid=pid,
        children_pids=children_pids,
        type=process_type,
        log_file=log_file,
        start_time=start_time_str,
    )
    logger.info(
        f"Started subprocess with PID {pid}. Logs are being written to {log_file}."
    )
    save_registry(
        process_registry=process_registry
    )  # Persist registry after adding process
    if process_mode == ILAB_PROCESS_MODES.ATTACHED:
        with open(log_file, "a+") as log:
            sys.stdout = Tee(log)
            sys.stderr = sys.stdout
            try:
                target(**kwargs)  # Call the function
            finally:
                # Restore the original stdout and stderr after the function completes
                process_registry.processes.pop(str(local_uuid))
                save_registry(process_registry=process_registry)
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__


def all_processes_running(pids: list[int]) -> bool:
    """
    Returns if a process and all of its children are still running
    Args:
        pids (list): a list of all PIDs to check
    """
    return all(psutil.pid_exists(pid) for pid in pids)


def stop_process(local_uuid, remove=True):
    """
    Stop a running process.

    Args:
        local_uuid (str): uuid of the process to stop.
    """
    process_registry = load_registry()
    # we should kill the parent process, and also children processes.
    pid = process_registry.processes[local_uuid]["pid"]
    children_pids = process_registry.processes[local_uuid]["children_pids"]
    all_processes = [pid] + children_pids
    for process in all_processes:
        try:
            os.kill(process, signal.SIGKILL)
            logger.info(f"Process {process} terminated.")
        except (ProcessLookupError, PermissionError):
            logger.warning(
                f"Process {process} was not running or could not be stopped."
            )
    if remove:
        process_registry.processes.pop(local_uuid, None)
    else:
        process_registry.processes[local_uuid]["done"] = True
    save_registry(process_registry=process_registry)


def list_processes():
    process_registry = load_registry()
    if not process_registry:
        logger.info("No processes currently in the registry.")
        return

    list_of_processes = []
    for local_uuid, entry in process_registry.processes.items():
        all_pids = [entry["pid"]] + entry["children_pids"]
        if not all_processes_running(all_pids):
            remove = entry["done"]
            stop_process(local_uuid=local_uuid, remove=remove)
            if remove:
                continue
        now = datetime.now()

        # Calculate runtime
        runtime = now - datetime.fromisoformat(entry.get("start_time"))
        # Convert timedelta to a human-readable string (HH:MM:SS)
        total_seconds = int(runtime.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        runtime_str = f"{hours:02}:{minutes:02}:{seconds:02}"
        list_of_processes.append(
            (
                entry.get("type"),
                entry.get("pid"),
                local_uuid,
                entry.get("log_file"),
                runtime_str,
            )
        )

    return list_of_processes


def attach_process(local_uuid: str):
    """
    Attach to a running process and display its output in real-time.

    Args:
        local_uuid (str): UUID of the process to attach to
    """
    process_registry = load_registry()
    if local_uuid not in process_registry.processes:
        logger.warning("Process not found.")
        return

    process_info = process_registry.processes[local_uuid]
    log_file = process_info["log_file"]

    if not os.path.exists(log_file):
        logger.warning(
            "Log file not found. The process may not have started logging yet."
        )
        return

    logger.info(f"Attaching to process {local_uuid}. Press Ctrl+C to detach and kill.")
    all_pids = [process_info["pid"]] + process_info["children_pids"]
    if not all_processes_running(all_pids):
        return
    try:
        with open(log_file, "a+") as log:
            log.seek(0, os.SEEK_END)  # Move to the end of the log file
            while all_processes_running(all_pids):
                line = log.readline()
                # Check for non-empty and non-whitespace-only lines
                if line.strip():
                    print(line.strip())
                else:
                    time.sleep(0.1)  # Wait briefly before trying again
    except KeyboardInterrupt:
        logger.info("\nDetaching from and killing process.")
    finally:
        stop_process(local_uuid=local_uuid)


def get_latest_process() -> str | None:
    """
    Returns the last process added to the registry to quickly allow users to attach to it.

    Returns:
        last_key (str): a string UUID to attach to
    """
    process_registry = load_registry()
    keys = process_registry.processes.keys()
    # no processes
    if len(keys) == 0:
        return None
    last_key = list(process_registry.processes.keys())[-1]
    assert isinstance(last_key, str)
    return last_key
