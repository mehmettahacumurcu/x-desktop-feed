import ctypes
import sys
from typing import Any, Protocol


_ERROR_ALREADY_EXISTS = 183
_MUTEX_NAME = r"Local\XDesktopFeed.Internship.SingleInstance"


class _Kernel32(Protocol):
    def CreateMutexW(self, security: object, initial_owner: bool, name: str) -> int: ...

    def GetLastError(self) -> int: ...

    def ReleaseMutex(self, handle: int) -> bool: ...

    def CloseHandle(self, handle: int) -> bool: ...


class _CtypesKernel32:
    def __init__(self) -> None:
        library: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        library.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        library.CreateMutexW.restype = ctypes.c_void_p
        library.ReleaseMutex.argtypes = [ctypes.c_void_p]
        library.ReleaseMutex.restype = ctypes.c_int
        library.CloseHandle.argtypes = [ctypes.c_void_p]
        library.CloseHandle.restype = ctypes.c_int
        self._library = library

    def CreateMutexW(self, security: object, initial_owner: bool, name: str) -> int:
        return int(self._library.CreateMutexW(security, initial_owner, name) or 0)

    @staticmethod
    def GetLastError() -> int:
        return ctypes.get_last_error()

    def ReleaseMutex(self, handle: int) -> bool:
        return bool(self._library.ReleaseMutex(handle))

    def CloseHandle(self, handle: int) -> bool:
        return bool(self._library.CloseHandle(handle))


def _load_kernel32() -> _Kernel32:
    if sys.platform != "win32":
        raise RuntimeError("X Desktop Feed single-instance startup requires Windows")
    return _CtypesKernel32()


class SingleInstanceGuard:
    """Owns the per-user Windows mutex for one desktop-app lifetime."""

    def __init__(self, kernel32: _Kernel32, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle: int | None = handle

    @classmethod
    def acquire(cls, *, kernel32: _Kernel32 | None = None) -> "SingleInstanceGuard | None":
        api = kernel32 or _load_kernel32()
        handle = api.CreateMutexW(None, True, _MUTEX_NAME)
        if not handle:
            error = api.GetLastError()
            raise OSError(error, f"Could not create the application mutex (Windows error {error})")
        if api.GetLastError() == _ERROR_ALREADY_EXISTS:
            api.CloseHandle(handle)
            return None
        return cls(api, handle)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if not self._kernel32.ReleaseMutex(handle):
                error = self._kernel32.GetLastError()
                raise OSError(
                    error,
                    f"Could not release the application mutex (Windows error {error})",
                )
        finally:
            self._kernel32.CloseHandle(handle)
