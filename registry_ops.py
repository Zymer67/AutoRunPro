from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Iterable, Optional

try:
    import winreg  # type: ignore
except ImportError:  # Allows syntax checks and import on non-Windows hosts.
    winreg = None  # type: ignore

from models import AutostartEntry

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_ONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
STARTUP_APPROVED_BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
DISABLED_RUNONCE_BASE = r"Software\AutostartManager\DisabledRunOnce"


class RegistryUnavailableError(RuntimeError):
    pass


def _require_windows() -> None:
    if winreg is None:
        raise RegistryUnavailableError("Операции с реестром доступны только в Windows.")


def _root(hive: str):
    _require_windows()
    roots = {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
    }
    if hive not in roots:
        raise ValueError(f"Неизвестный раздел реестра: {hive}")
    return roots[hive]


def _view_flag(view: str) -> int:
    _require_windows()
    if view == "64":
        return getattr(winreg, "KEY_WOW64_64KEY", 0)
    if view == "32":
        return getattr(winreg, "KEY_WOW64_32KEY", 0)
    return 0


def _is_64_bit_windows() -> bool:
    return os.environ.get("PROCESSOR_ARCHITEW6432") is not None or os.environ.get(
        "PROCESSOR_ARCHITECTURE", ""
    ).lower() in {"amd64", "arm64", "ia64"}


def _expand_environment(value: Any) -> str:
    if not isinstance(value, str):
        return str(value)
    if os.name == "nt":
        try:
            import ctypes

            expand = ctypes.windll.kernel32.ExpandEnvironmentStringsW
            expand.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            expand.restype = ctypes.c_uint
            needed = expand(value, None, 0)
            if needed:
                buffer = ctypes.create_unicode_buffer(needed)
                expand(value, buffer, needed)
                return buffer.value
        except Exception:
            pass
    return os.path.expandvars(value)


def _approval_state(hive: str, subkey: str, value_name: str, view: str = "native") -> Optional[bool]:
    """Return True/False when StartupApproved has an entry, otherwise None."""
    _require_windows()
    try:
        with winreg.OpenKey(
            _root(hive), subkey, 0, winreg.KEY_READ | _view_flag(view)
        ) as key:
            data, value_type = winreg.QueryValueEx(key, value_name)
        if value_type != winreg.REG_BINARY or not isinstance(data, (bytes, bytearray)) or not data:
            return None
        # Windows uses an even leading state byte for enabled and an odd one
        # for disabled. 0x02 and 0x03 are the common values.
        return (data[0] % 2) == 0
    except FileNotFoundError:
        return None
    except OSError:
        return None


def get_startupapproved_enabled(
    value_name: str,
    subkey_name: str,
    preferred_hive: str = "HKCU",
    fallback_hives: Iterable[str] = (),
    view: str = "native",
) -> bool:
    """Read StartupApproved; missing state means enabled by Windows default."""
    _require_windows()
    seen: set[str] = set()
    for hive in (preferred_hive, *fallback_hives):
        if hive in seen:
            continue
        seen.add(hive)
        state = _approval_state(
            hive,
            STARTUP_APPROVED_BASE + "\\" + subkey_name,
            value_name,
            view,
        )
        if state is not None:
            return state
    return True


def set_startupapproved_enabled(
    value_name: str,
    subkey_name: str,
    enabled: bool,
    hive: str = "HKCU",
    view: str = "native",
) -> None:
    _require_windows()
    subkey = STARTUP_APPROVED_BASE + "\\" + subkey_name
    access = winreg.KEY_SET_VALUE | _view_flag(view)
    with winreg.CreateKeyEx(_root(hive), subkey, 0, access) as key:
        state = 0x02 if enabled else 0x03
        # FILETIME (100 ns ticks since 1601-01-01 UTC) is kept in the final
        # eight bytes for disabled entries. Enabled entries commonly use zero.
        filetime = 0
        if not enabled:
            filetime = int((time.time() + 11644473600) * 10_000_000)
        blob = bytes((state, 0, 0, 0)) + filetime.to_bytes(8, "little", signed=False)
        winreg.SetValueEx(key, value_name, 0, winreg.REG_BINARY, blob)


def _backup_subkey(view: str, original_subkey: str) -> str:
    leaf = original_subkey.rsplit("\\", 1)[-1]
    view_name = "32" if view == "32" else "64" if view == "64" else "native"
    return f"{DISABLED_RUNONCE_BASE}\\{view_name}\\{leaf}"


def _encode_registry_data(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        value = list(value)
    return {"kind": "json", "value": value}


def _decode_registry_data(payload: dict[str, Any]) -> Any:
    if payload.get("kind") == "bytes":
        return base64.b64decode(payload["value"])
    return payload.get("value")


def _read_value(hive: str, subkey: str, name: str, view: str) -> tuple[Any, int]:
    with winreg.OpenKey(_root(hive), subkey, 0, winreg.KEY_QUERY_VALUE | _view_flag(view)) as key:
        return winreg.QueryValueEx(key, name)


def _write_value(hive: str, subkey: str, name: str, value: Any, value_type: int, view: str) -> None:
    with winreg.CreateKeyEx(
        _root(hive), subkey, 0, winreg.KEY_SET_VALUE | _view_flag(view)
    ) as key:
        winreg.SetValueEx(key, name, 0, value_type, value)


def _delete_value(hive: str, subkey: str, name: str, view: str) -> None:
    with winreg.OpenKey(_root(hive), subkey, 0, winreg.KEY_SET_VALUE | _view_flag(view)) as key:
        winreg.DeleteValue(key, name)


def _stash_runonce(entry: AutostartEntry) -> None:
    assert entry.registry_hive and entry.registry_subkey and entry.registry_value_name
    value, value_type = _read_value(
        entry.registry_hive,
        entry.registry_subkey,
        entry.registry_value_name,
        entry.registry_view,
    )
    backup = _backup_subkey(entry.registry_view, entry.registry_subkey)
    record = {
        "original_subkey": entry.registry_subkey,
        "value_type": int(value_type),
        "data": _encode_registry_data(value),
    }
    _write_value(
        entry.registry_hive,
        backup,
        entry.registry_value_name,
        json.dumps(record, ensure_ascii=False),
        winreg.REG_SZ,
        entry.registry_view,
    )
    try:
        _delete_value(
            entry.registry_hive,
            entry.registry_subkey,
            entry.registry_value_name,
            entry.registry_view,
        )
    except Exception:
        # If the original value could not be removed, discard the backup so
        # the next scan does not show a duplicate disabled entry.
        try:
            _delete_value(entry.registry_hive, backup, entry.registry_value_name, entry.registry_view)
        except Exception:
            pass
        raise


def _restore_runonce(entry: AutostartEntry) -> None:
    assert entry.registry_hive and entry.registry_value_name and entry.backup_subkey
    record_text, _ = _read_value(
        entry.registry_hive,
        entry.backup_subkey,
        entry.registry_value_name,
        entry.registry_view,
    )
    record = json.loads(record_text)
    original_value = _decode_registry_data(record["data"])
    _write_value(
        entry.registry_hive,
        record["original_subkey"],
        entry.registry_value_name,
        original_value,
        int(record["value_type"]),
        entry.registry_view,
    )
    _delete_value(
        entry.registry_hive,
        entry.backup_subkey,
        entry.registry_value_name,
        entry.registry_view,
    )


def scan_registry_entries() -> list[AutostartEntry]:
    _require_windows()
    is_64 = _is_64_bit_windows()
    descriptors: list[tuple[str, str, str, str, str]] = [
        ("HKCU", RUN_KEY, "native", "Run", "Run"),
        ("HKCU", RUN_ONCE_KEY, "native", "RunOnce", "RunOnce"),
    ]
    if is_64:
        descriptors.extend(
            [
                ("HKLM", RUN_KEY, "64", "Run", "Run"),
                ("HKLM", RUN_ONCE_KEY, "64", "RunOnce", "RunOnce"),
                (
                    "HKLM",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
                    "64",
                    "Run32",
                    "Run",
                ),
            ]
        )
    else:
        descriptors.extend(
            [
                ("HKLM", RUN_KEY, "native", "Run", "Run"),
                ("HKLM", RUN_ONCE_KEY, "native", "RunOnce", "RunOnce"),
            ]
        )

    results: list[AutostartEntry] = []
    seen: set[tuple[str, str, str, str]] = set()

    for hive, subkey, view, approval_subkey, kind in descriptors:
        try:
            with winreg.OpenKey(
                _root(hive), subkey, 0, winreg.KEY_READ | _view_flag(view)
            ) as key:
                index = 0
                while True:
                    try:
                        value_name, data, value_type = winreg.EnumValue(key, index)
                        index += 1
                    except OSError:
                        break
                    if not value_name or data is None:
                        continue
                    identity = (hive, subkey.lower(), view, value_name.lower())
                    if identity in seen:
                        continue
                    seen.add(identity)
                    command = _expand_environment(data)
                    if kind == "RunOnce":
                        enabled = True
                        approval_name = None
                        approval_hive = None
                    else:
                        approval_name = value_name
                        # Use the matching hive for system-wide Run entries;
                        # fall back to the per-user approval when it is the
                        # only state available on a particular Windows build.
                        approval_hive = hive
                        fallback = ("HKCU",) if hive == "HKLM" else ()
                        actual_approval_subkey = approval_subkey
                        enabled = get_startupapproved_enabled(
                            approval_name,
                            actual_approval_subkey,
                            preferred_hive=approval_hive,
                            fallback_hives=fallback,
                        )
                    view_label = f" [{view}-bit view]" if view in {"32", "64"} else ""
                    source = f"{hive}\\{subkey}{view_label}"
                    results.append(
                        AutostartEntry(
                            name=str(value_name),
                            command=command,
                            source_type="registry",
                            source_path=source,
                            enabled=enabled,
                            registry_hive=hive,
                            registry_subkey=subkey,
                            registry_value_name=str(value_name),
                            registry_view=view,
                            registry_value_type=int(value_type),
                            is_run_once=(kind == "RunOnce"),
                            approval_hive=approval_hive,
                            approval_subkey=approval_subkey if approval_name else None,
                            approval_value_name=approval_name,
                        )
                    )
        except FileNotFoundError:
            continue
        except OSError:
            # One inaccessible key must not prevent the remaining sources
            # from being enumerated.
            continue

    # RunOnce entries disabled by this application are retained in a private
    # backup key and still appear in the same list.
    for hive, subkey, view, _approval_subkey, kind in descriptors:
        if kind != "RunOnce":
            continue
        backup = _backup_subkey(view, subkey)
        try:
            with winreg.OpenKey(
                _root(hive), backup, 0, winreg.KEY_READ | _view_flag(view)
            ) as key:
                index = 0
                while True:
                    try:
                        value_name, record_text, _value_type = winreg.EnumValue(key, index)
                        index += 1
                    except OSError:
                        break
                    record = {}
                    try:
                        parsed_record = json.loads(record_text)
                        record = parsed_record if isinstance(parsed_record, dict) else {}
                        data = _decode_registry_data(record["data"])
                        command = _expand_environment(data)
                    except Exception:
                        record = {}
                        command = "[Не удалось прочитать сохранённую команду]"
                    source = f"{hive}\\{record.get('original_subkey', subkey)} [отключено]"
                    results.append(
                        AutostartEntry(
                            name=str(value_name),
                            command=command,
                            source_type="registry",
                            source_path=source,
                            enabled=False,
                            registry_hive=hive,
                            registry_subkey=record.get("original_subkey", subkey),
                            registry_value_name=str(value_name),
                            registry_view=view,
                            is_run_once=True,
                            run_once_stashed=True,
                            backup_subkey=backup,
                        )
                    )
        except FileNotFoundError:
            continue
        except OSError:
            continue

    return results


def set_registry_entry_enabled(entry: AutostartEntry, enabled: bool) -> None:
    if entry.is_run_once:
        if enabled and entry.run_once_stashed:
            _restore_runonce(entry)
        elif not enabled and not entry.run_once_stashed:
            _stash_runonce(entry)
        return
    if not (entry.approval_subkey and entry.approval_value_name and entry.approval_hive):
        raise RuntimeError("Для этой записи не найден поддерживаемый переключатель StartupApproved.")
    set_startupapproved_enabled(
        entry.approval_value_name,
        entry.approval_subkey,
        enabled,
        hive=entry.approval_hive,
        view=entry.approval_view,
    )


def delete_registry_entry(entry: AutostartEntry) -> None:
    if not entry.registry_hive or not entry.registry_value_name:
        raise ValueError("Недостаточно данных для удаления записи реестра.")
    if entry.is_run_once and entry.run_once_stashed and entry.backup_subkey:
        _delete_value(
            entry.registry_hive,
            entry.backup_subkey,
            entry.registry_value_name,
            entry.registry_view,
        )
        return
    if not entry.registry_subkey:
        raise ValueError("Не указан ключ реестра.")
    _delete_value(
        entry.registry_hive,
        entry.registry_subkey,
        entry.registry_value_name,
        entry.registry_view,
    )


def add_current_user_run(executable_path: str) -> str:
    _require_windows()
    if not os.path.isfile(executable_path):
        raise FileNotFoundError(executable_path)
    base_name = os.path.splitext(os.path.basename(executable_path))[0] or "Application"
    value_name = base_name
    suffix = 2
    while True:
        try:
            with winreg.OpenKey(_root("HKCU"), RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                winreg.QueryValueEx(key, value_name)
            value_name = f"{base_name} ({suffix})"
            suffix += 1
        except FileNotFoundError:
            break
        except OSError:
            # If the key itself is unavailable, CreateKeyEx below will provide
            # a meaningful error rather than looping indefinitely.
            break
    command = f'"{executable_path}"'
    _write_value("HKCU", RUN_KEY, value_name, command, winreg.REG_SZ, "native")
    return value_name
