from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class ScheduledTaskInfo:
    name: str
    path: str
    command: str
    executable_path: Optional[str]
    enabled: bool
    trigger_labels: tuple[str, ...]


def _expand_environment(value: str) -> str:
    if not value:
        return ""
    if os.name == "nt":
        try:
            import ctypes

            fn = ctypes.windll.kernel32.ExpandEnvironmentStringsW
            needed = fn(value, None, 0)
            if needed:
                buffer = ctypes.create_unicode_buffer(needed)
                fn(value, buffer, needed)
                return buffer.value
        except Exception:
            pass
    return os.path.expandvars(value)


def _quote_executable(path: str) -> str:
    path = _expand_environment(path.strip())
    if not path:
        return ""
    if path.startswith('"'):
        return path
    if any(char.isspace() for char in path):
        return f'"{path}"'
    return path


def enumerate_startup_tasks() -> list[ScheduledTaskInfo]:
    """Enumerate tasks with boot or logon triggers using Task Scheduler COM."""
    if os.name != "nt":
        return []

    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Для чтения планировщика требуется pywin32.") from exc

    pythoncom.CoInitialize()
    found: list[ScheduledTaskInfo] = []
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        root = service.GetFolder("\\")

        def walk(folder) -> None:
            try:
                tasks = folder.GetTasks(1)  # TASK_ENUM_HIDDEN
            except Exception:
                tasks = []
            for task in tasks:
                try:
                    definition = task.Definition
                    trigger_types: list[int] = []
                    for trigger in definition.Triggers:
                        try:
                            trigger_type = int(trigger.Type)
                        except Exception:
                            continue
                        if trigger_type in (8, 9):
                            trigger_types.append(trigger_type)
                    if not trigger_types:
                        continue

                    labels = tuple(
                        label
                        for trigger_type, label in ((8, "При запуске системы"), (9, "При входе пользователя"))
                        if trigger_type in trigger_types
                    )
                    command_parts: list[str] = []
                    executable_path: Optional[str] = None
                    try:
                        for action in definition.Actions:
                            action_type = int(action.Type)
                            if action_type == 0:  # TASK_ACTION_EXEC
                                action_path = _expand_environment(str(action.Path or ""))
                                arguments = str(action.Arguments or "").strip()
                                if action_path and not executable_path:
                                    executable_path = action_path
                                part = _quote_executable(action_path)
                                if arguments:
                                    part = f"{part} {arguments}".strip()
                                if part:
                                    command_parts.append(part)
                            else:
                                command_parts.append(f"[Действие планировщика, тип {action_type}]")
                    except Exception:
                        command_parts = ["[Не удалось прочитать действие задачи]"]

                    task_path = str(getattr(task, "Path", "") or "")
                    task_name = str(getattr(task, "Name", "") or task_path.rsplit("\\", 1)[-1])
                    enabled = bool(getattr(task, "Enabled", True))
                    found.append(
                        ScheduledTaskInfo(
                            name=task_name,
                            path=task_path,
                            command=" ; ".join(command_parts) or "[Нет доступного действия]",
                            executable_path=executable_path,
                            enabled=enabled,
                            trigger_labels=labels,
                        )
                    )
                except Exception:
                    # A malformed or protected task should not abort enumeration.
                    continue

            try:
                subfolders = folder.GetFolders(0)
            except Exception:
                subfolders = []
            for subfolder in subfolders:
                try:
                    walk(subfolder)
                except Exception:
                    continue

        walk(root)
    finally:
        pythoncom.CoUninitialize()
    return found


def _run_schtasks(arguments: list[str]) -> None:
    command = ["schtasks.exe", *arguments]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=creationflags,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Операция отклонена Windows.").strip()
        raise RuntimeError(details)


def set_task_enabled(task_path: str, enabled: bool) -> None:
    action = "/ENABLE" if enabled else "/DISABLE"
    _run_schtasks(["/Change", "/TN", task_path, action])


def delete_task(task_path: str) -> None:
    _run_schtasks(["/Delete", "/TN", task_path, "/F"])
