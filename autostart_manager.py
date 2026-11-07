from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from icon_extractor import IconExtractor
from models import AutostartEntry
import registry_ops
import task_ops

def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def expand_windows_environment(value: str) -> str:
    if not value:
        return ""
    if os.name == "nt":
        try:
            fn = ctypes.windll.kernel32.ExpandEnvironmentStringsW
            needed = fn(value, None, 0)
            if needed:
                buffer = ctypes.create_unicode_buffer(needed)
                fn(value, buffer, needed)
                return buffer.value
        except Exception:
            pass
    return os.path.expandvars(value)


def executable_from_command(command: str) -> Optional[str]:
    """Best-effort extraction of the executable path from a Windows command line."""
    if not command:
        return None
    value = expand_windows_environment(command.strip())
    if not value:
        return None

    if value.startswith('"'):
        closing = value.find('"', 1)
        if closing > 1:
            return value[1:closing]
    if value.startswith("'"):
        closing = value.find("'", 1)
        if closing > 1:
            return value[1:closing]

    # Unquoted Windows paths can contain spaces. Stop at the first executable
    # extension rather than splitting at the first whitespace.
    match = re.search(r"\.(?:exe|com|bat|cmd|ps1|vbs|js|scr|msc)(?=\s|,|$)", value, re.IGNORECASE)
    if match:
        return value[: match.end()].strip().strip('"')

    first = value.split(None, 1)[0].strip('"') if value.split(None, 1) else ""
    if first:
        resolved = shutil.which(first)
        if resolved:
            return resolved
    return first or None


def _format_command(target: str, arguments: str = "") -> str:
    target = expand_windows_environment(target.strip())
    if not target:
        return arguments.strip()
    quoted = target if target.startswith('"') else (f'"{target}"' if any(c.isspace() for c in target) else target)
    return f"{quoted} {arguments.strip()}".strip()


class AutostartManager:
    def __init__(self) -> None:
        self.is_admin = is_windows_admin()
        self.icons = IconExtractor(size=32)
        self.last_scan_warnings: list[str] = []

    def scan_all(self) -> list[AutostartEntry]:
        if os.name != "nt":
            raise RuntimeError("Это приложение предназначено для Windows 10/11.")

        self.last_scan_warnings = []
        entries: list[AutostartEntry] = []

        try:
            entries.extend(registry_ops.scan_registry_entries())
        except Exception as exc:
            self.last_scan_warnings.append(f"Реестр: {exc}")

        entries.extend(self._scan_startup_folders())

        try:
            for task in task_ops.enumerate_startup_tasks():
                entries.append(
                    AutostartEntry(
                        name=task.name,
                        command=task.command,
                        source_type="task",
                        source_path=f"Планировщик задач: {task.path}",
                        enabled=task.enabled,
                        executable_path=task.executable_path or executable_from_command(task.command),
                        task_path=task.path,
                    )
                )
        except Exception as exc:
            self.last_scan_warnings.append(f"Планировщик задач: {exc}")

        for entry in entries:
            if not entry.executable_path:
                entry.executable_path = executable_from_command(entry.command)
            entry.icon_path = self.icons.icon_for(entry.executable_path)
            self._set_capabilities(entry)

        entries.sort(key=lambda item: (item.name or "").casefold())
        return entries

    def _scan_startup_folders(self) -> list[AutostartEntry]:
        folders: list[tuple[str, str]] = []
        appdata = os.environ.get("APPDATA")
        program_data = os.environ.get("ProgramData")
        if appdata:
            folders.append(
                (
                    "user",
                    os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
                )
            )
        if program_data:
            folders.append(
                (
                    "common",
                    os.path.join(program_data, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
                )
            )

        results: list[AutostartEntry] = []
        for scope, folder in folders:
            try:
                if not os.path.isdir(folder):
                    continue
                for item in os.scandir(folder):
                    try:
                        if not item.is_file():
                            continue
                        if item.name.lower() == "desktop.ini":
                            continue
                        command, executable = self._startup_file_command(item.path)
                        approval_hive = "HKCU" if scope == "user" or not self.is_admin else "HKLM"
                        try:
                            enabled = registry_ops.get_startupapproved_enabled(
                                item.name,
                                "StartupFolder",
                                preferred_hive=approval_hive,
                                fallback_hives=("HKCU", "HKLM"),
                            )
                        except Exception:
                            enabled = True
                        label = "Папка автозагрузки пользователя" if scope == "user" else "Общая папка автозагрузки"
                        results.append(
                            AutostartEntry(
                                name=Path(item.name).stem,
                                command=command,
                                source_type="folder",
                                source_path=item.path,
                                enabled=enabled,
                                executable_path=executable,
                                folder_path=item.path,
                                source_scope=scope,
                                approval_hive=approval_hive,
                                approval_subkey="StartupFolder",
                                approval_value_name=item.name,
                            )
                        )
                    except OSError as exc:
                        self.last_scan_warnings.append(f"Не удалось прочитать {item.path}: {exc}")
            except OSError as exc:
                self.last_scan_warnings.append(f"Папка {folder}: {exc}")
        return results

    def _startup_file_command(self, path: str) -> tuple[str, Optional[str]]:
        suffix = Path(path).suffix.lower()
        if suffix == ".lnk":
            try:
                import pythoncom  # type: ignore
                import win32com.client  # type: ignore

                pythoncom.CoInitialize()
                try:
                    shell = win32com.client.Dispatch("WScript.Shell")
                    shortcut = shell.CreateShortcut(path)
                    target = str(shortcut.TargetPath or "")
                    arguments = str(shortcut.Arguments or "")
                    command = _format_command(target, arguments)
                    return command or path, target or None
                finally:
                    pythoncom.CoUninitialize()
            except Exception:
                return path, None
        if suffix == ".url":
            return path, None
        return f'"{path}"' if any(c.isspace() for c in path) else path, path

    def _set_capabilities(self, entry: AutostartEntry) -> None:
        reason: Optional[str] = None
        if entry.registry_hive == "HKLM" and not self.is_admin:
            reason = "Запись HKLM доступна только для чтения без прав администратора."
        elif entry.source_scope == "common" and not self.is_admin:
            reason = "Общая папка автозагрузки доступна для изменения только с правами администратора."
        elif (
            entry.source_type == "task"
            and (entry.task_path or "").lower().startswith("\\microsoft\\windows\\")
            and not self.is_admin
        ):
            reason = "Системная задача обычно требует прав администратора."

        entry.can_toggle = reason is None
        entry.can_delete = reason is None
        entry.disabled_reason = reason

    def set_enabled(self, entry: AutostartEntry, enabled: bool) -> None:
        if not entry.can_toggle:
            raise PermissionError(entry.disabled_reason or "Изменение этой записи недоступно.")
        if entry.source_type == "registry":
            registry_ops.set_registry_entry_enabled(entry, enabled)
        elif entry.source_type == "folder":
            if not (entry.approval_value_name and entry.approval_subkey and entry.approval_hive):
                raise RuntimeError("Не удалось определить запись StartupApproved для ярлыка.")
            registry_ops.set_startupapproved_enabled(
                entry.approval_value_name,
                entry.approval_subkey,
                enabled,
                hive=entry.approval_hive,
            )
        elif entry.source_type == "task":
            if not entry.task_path:
                raise RuntimeError("У задачи отсутствует полный путь.")
            task_ops.set_task_enabled(entry.task_path, enabled)
        else:
            raise ValueError(f"Неизвестный источник: {entry.source_type}")

    def remove(self, entry: AutostartEntry) -> None:
        if not entry.can_delete:
            raise PermissionError(entry.disabled_reason or "Удаление этой записи недоступно.")
        if entry.source_type == "registry":
            registry_ops.delete_registry_entry(entry)
        elif entry.source_type == "folder":
            if not entry.folder_path:
                raise RuntimeError("Не указан путь к файлу автозагрузки.")
            os.remove(entry.folder_path)
        elif entry.source_type == "task":
            if not entry.task_path:
                raise RuntimeError("У задачи отсутствует полный путь.")
            task_ops.delete_task(entry.task_path)
        else:
            raise ValueError(f"Неизвестный источник: {entry.source_type}")

    def add_executable(self, executable_path: str) -> str:
        path = os.path.abspath(executable_path)
        if Path(path).suffix.lower() != ".exe":
            raise ValueError("Можно добавить только файл .exe.")
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        return registry_ops.add_current_user_run(path)
