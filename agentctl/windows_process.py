"""Win32 job objects keep managed subprocess trees under Glass's control."""
import ctypes
from ctypes import wintypes


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
CREATE_SUSPENDED = 0x00000004
INVALID_HANDLE = ctypes.c_void_p(-1).value


def api(name, result, *arguments):
    function = getattr(kernel, name)
    function.restype = result
    function.argtypes = arguments
    return function


close_handle = api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
create_job = api("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
set_job = api("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
assign_job = api("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
snapshot_threads = api("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
open_thread = api("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
open_process = api("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
resume_thread = api("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
local_free = api("LocalFree", ctypes.c_void_p, ctypes.c_void_p)


def split_windows_command(command):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    parse = shell.CommandLineToArgvW
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    count = ctypes.c_int()
    values = parse(command, ctypes.byref(count))
    if not values:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return values[:count.value]
    finally:
        local_free(values)


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                ("process_id", wintypes.DWORD), ("base_priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]


first_thread = api("Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
next_thread = api("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))


def resume_primary(pid):
    snapshot = snapshot_threads(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry(size=ctypes.sizeof(ThreadEntry))
        found = first_thread(snapshot, ctypes.byref(entry))
        while found:
            if entry.process_id == pid:
                thread = open_thread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if resume_thread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    close_handle(thread)
            entry.size = ctypes.sizeof(ThreadEntry)
            found = next_thread(snapshot, ctypes.byref(entry))
        raise OSError("Could not locate the suspended Windows process thread.")
    finally:
        close_handle(snapshot)


class Job:
    def __init__(self):
        self.handle = create_job(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def start(self, process):
        handle = open_process(0x0101, False, process.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not assign_job(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(handle)
        resume_primary(process.pid)

    def close(self):
        if self.handle:
            close_handle(self.handle)
            self.handle = None
