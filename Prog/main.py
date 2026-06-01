"""Akkuteszter — Labor műszerfal belépési pont."""
import sys
from PySide6.QtWidgets import QApplication
from Prog.gui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Akkuteszter")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
