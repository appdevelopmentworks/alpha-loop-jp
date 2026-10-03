"""Bounded subprocesses with descendant cleanup on Windows and POSIX."""
import os
import signal
import subprocess


def _windows_job(child):
    """Closing the job kills descendants even if the immediate parent has exited."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(child._handle))):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return kernel, handle


def run_bounded(command, timeout, **kwargs):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options, **kwargs)
    job = None
    try:
        if os.name == "nt":
            try:
                job = _windows_job(child)
            except OSError:
                child.kill()
                child.communicate(timeout=15)
                raise
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            if not job[0].TerminateJobObject(job[1], 1):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=15)
        raise
    finally:
        if job:
            job[0].CloseHandle(job[1])
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
