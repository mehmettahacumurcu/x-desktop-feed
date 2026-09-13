import sys

from PySide6.QtWidgets import QMessageBox

from xfeed.app import ApplicationAlreadyRunning, create_application


def main() -> int:
    try:
        application, window = create_application(sys.argv)
    except ApplicationAlreadyRunning as error:
        QMessageBox.information(None, "X Desktop Feed", str(error))
        return 0
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
