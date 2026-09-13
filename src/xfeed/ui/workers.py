from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal


class Worker(QRunnable, QObject):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], object]) -> None:
        QRunnable.__init__(self)
        QObject.__init__(self)
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as error:
            self.failed.emit(str(error))
            return
        self.result.emit(result)
