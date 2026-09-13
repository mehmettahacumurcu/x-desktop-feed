from xfeed.single_instance import SingleInstanceGuard


class Kernel32Double:
    def __init__(self, *, handle: int = 41, last_error: int = 0) -> None:
        self.handle = handle
        self.last_error = last_error
        self.created: list[tuple[object, bool, str]] = []
        self.released: list[int] = []
        self.closed: list[int] = []

    def CreateMutexW(self, security, initial_owner: bool, name: str) -> int:
        self.created.append((security, initial_owner, name))
        return self.handle

    def GetLastError(self) -> int:
        return self.last_error

    def ReleaseMutex(self, handle: int) -> bool:
        self.released.append(handle)
        return True

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def test_guard_owns_named_windows_mutex_until_idempotent_release() -> None:
    kernel32 = Kernel32Double()

    guard = SingleInstanceGuard.acquire(kernel32=kernel32)

    assert guard is not None
    assert kernel32.created == [(None, True, r"Local\XDesktopFeed.Internship.SingleInstance")]
    guard.release()
    guard.release()
    assert kernel32.released == [41]
    assert kernel32.closed == [41]


def test_guard_refuses_existing_mutex_without_releasing_another_owner() -> None:
    kernel32 = Kernel32Double(last_error=183)

    guard = SingleInstanceGuard.acquire(kernel32=kernel32)

    assert guard is None
    assert kernel32.released == []
    assert kernel32.closed == [41]
