"""Read-only process liveness. Unknown/access-denied is conservatively alive."""
import os
def assert_alive(pid):
    if not isinstance(pid,int) or pid <= 0: raise ProcessLookupError(pid)
    if os.name != 'nt':
        try: os.kill(pid,0)
        except PermissionError: return
        return
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        if ctypes.get_last_error()==87: raise ProcessLookupError(pid)
        return
    try:
        code=wintypes.DWORD()
        if kernel.GetExitCodeProcess(handle,ctypes.byref(code)) and code.value!=259:
            raise ProcessLookupError(pid)
    finally: kernel.CloseHandle(handle)
