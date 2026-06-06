"""Akkuteszter — Labor műszerfal belépési pont."""
import shutil
import sys
from PySide6.QtWidgets import QApplication

from Prog import app_paths
from Prog.gui.main_window import MainWindow


def _ensure_local_config() -> None:
    """Ha local_config.yaml nem létezik, másolja a beágyazott template-t."""
    target = app_paths.local_config_path()
    if not target.exists():
        template = app_paths.local_config_template_path()
        if template.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(template), str(target))


def main() -> None:
    _ensure_local_config()
    app = QApplication(sys.argv)
    app.setApplicationName("Akkuteszter")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
