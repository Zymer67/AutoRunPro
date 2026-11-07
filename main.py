from __future__ import annotations

import os
import sys


def main() -> int:
    if os.name != "nt":
        print("Windows Autostart Manager запускается только в Windows 10/11.")
        return 1

    try:
        from ui.app import AutostartApp

        app = AutostartApp()
        app.mainloop()
        return 0
    except ImportError as exc:
        print("Не установлены зависимости. Выполните: py -m pip install -r requirements.txt")
        print(exc)
        return 2
    except Exception as exc:
        print(f"Не удалось запустить приложение: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
