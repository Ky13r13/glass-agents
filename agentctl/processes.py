"""Managed process trees and pipe events across POSIX and Windows."""
from contextlib import contextmanager
import os
import queue
import selectors
import shlex
import shutil
import signal
import subprocess
import threading


def native_argv(argv):
    if os.name != "nt":
        return argv
    candidate = argv[0] if os.path.splitext(argv[0])[1] else argv[0] + ".exe"
    executable = shutil.which(candidate)
    if not executable or os.path.splitext(executable)[1].lower() not in {".exe", ".com"}:
        raise OSError(f"Select a native Windows executable for {argv[0]}. Set the .exe path in Connections for providers.")
    return [executable, *argv[1:]]


def split_command(command):
    if os.name == "nt":
        from .windows_process import split_windows_command
        return split_windows_command(command)
    return shlex.split(command)


@contextmanager
def managed_process(argv, **kwargs):
    if os.name != "nt":
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
        with process:
            yield process, lambda grace: stop_group(process, grace)
        return

    from .windows_process import CREATE_SUSPENDED, Job

    argv = native_argv(argv)
    job = Job()
    try:
        process = subprocess.Popen(argv, creationflags=CREATE_SUSPENDED, **kwargs)
        with process:
            try:
                job.start(process)
            except OSError:
                process.kill()
                raise

            def stop(grace):
                job.close()
                process.wait(timeout=max(3, grace))

            try:
                yield process, stop
            finally:
                job.close()
    finally:
        job.close()


def stop_group(process, grace):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=3)


def pipe_chunks(process, check):
    pipes = {"stdout": process.stdout, "stderr": process.stderr}
    if os.name != "nt":
        with selectors.DefaultSelector() as selector:
            for name, pipe in pipes.items():
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                check()
                for key, _ in selector.select(timeout=0.2):
                    data = os.read(key.fd, 65536)
                    if data:
                        yield key.data, data
                    else:
                        selector.unregister(key.fileobj)
        return

    chunks = queue.Queue(maxsize=16)
    done = threading.Event()

    def offer(item):
        while not done.is_set():
            try:
                chunks.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def read(name, pipe):
        try:
            while not done.is_set():
                data = os.read(pipe.fileno(), 65536)
                if not data:
                    break
                offer((name, data))
        except OSError as exc:
            offer((name, exc))
        finally:
            offer((name, None))

    for name, pipe in pipes.items():
        threading.Thread(target=read, args=(name, pipe), daemon=True).start()
    try:
        remaining = len(pipes)
        while remaining:
            check()
            try:
                name, data = chunks.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                remaining -= 1
            elif isinstance(data, OSError):
                raise data
            else:
                yield name, data
    finally:
        done.set()
