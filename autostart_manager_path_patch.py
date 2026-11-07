"""Path parsing and AutostartEntry additions for autostart_manager.py.

Copy these definitions into autostart_manager.py (or import this module).
The function intentionally returns None when it cannot verify an existing
file, so the UI can use IconExtractor's gear placeholder instead of guessing.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional


# File/script types that may be the action target of a startup command.
_EXECUTABLE_SUFFIX_RE = re.compile(
    r"\.(?:exe|com|bat|cmd|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|msc|dll|cpl|scr|py)"
    r"(?=[\"'\s,]|$)",
    re.IGNORECASE,
)

_PLACEHOLDER_COMMANDS = {
    "[action]",
    "[действие]",
    "[no action]",
    "[нет действия]",
}


def _expand_windows_environment(value: str) -> str:
    """Expand %APPDATA%, %SystemRoot%, ~ and normal environment variables."""
    value = os.path.expandvars(os.path.expanduser(value.strip()))

    def replace_percent(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, os.environ.get(name.upper(), match.group(0)))

    value = re.sub(r"%([^%]+)%", replace_percent, value)

    # Task Scheduler occasionally reports the NT-style SystemRoot prefix.
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        value = re.sub(
            r"(?i)^\\SystemRoot(?=\\)",
            lambda _m: system_root,
            value,
        )

    # Remove NT device path prefixes when present.
    if value.startswith("\\\\?\\"):
        value = value[4:]
    elif value.startswith("\\??\\"):
        value = value[4:]
    return value.strip()


def _existing_file(candidate: str, base_dir: Optional[str] = None) -> Optional[str]:
    """Normalize a candidate and return it only when it exists as a file."""
    candidate = _expand_windows_environment(candidate).strip().strip('"').strip("'")
    if not candidate:
        return None

    # Remove common trailing punctuation from task/registry display strings.
    candidate = candidate.rstrip(";,\t ")
    if not candidate:
        return None

    if base_dir and not os.path.isabs(candidate):
        candidate = os.path.join(base_dir, candidate)

    try:
        candidate = os.path.normpath(candidate)
        # The application targets Windows; isfile prevents returning a folder
        # whose name happens to be the first fragment of an unquoted command.
        if os.path.exists(candidate) and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    except (OSError, ValueError):
        return None

    # Also resolve simple executable names from PATH, e.g. "powershell.exe".
    try:
        found = shutil.which(candidate)
        if found and os.path.isfile(found):
            return os.path.abspath(found)
    except (OSError, ValueError):
        pass
    return None


def _windows_command_line_tokens(command: str) -> list[str]:
    """Split a Windows command line with CommandLineToArgvW when available."""
    if os.name == "nt":
        argv_ptr = None
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            argc = ctypes.c_int(0)
            shell32.CommandLineToArgvW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p

            argv_ptr = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
            if argv_ptr and argc.value > 0:
                return [argv_ptr[i] for i in range(argc.value)]
        except Exception:
            pass
        finally:
            if argv_ptr:
                try:
                    kernel32.LocalFree(ctypes.cast(argv_ptr, ctypes.c_void_p))
                except Exception:
                    pass

    # Conservative fallback used for non-Windows syntax checks/tests only.
    # The extension-boundary scan below handles unquoted paths with spaces.
    import shlex
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def extract_executable_path(
    command_string: Optional[str],
    base_dir: Optional[str] = None,
) -> Optional[str]:
    """Extract the first existing executable/script/DLL path from a command.

    Handles, for example:
        "C:\\Program Files\\App\\app.exe" --startup
        C:\\Program Files\\App\\app.exe --startup
        "%LOCALAPPDATA%\\Discord\\Update.exe" --processStart Discord.exe
        C:\\Windows\\System32\\rundll32.exe shell32.dll,Control_RunDLL

    Returns None for placeholders such as ``[Action]``/``[Действие]``, empty
    commands, missing files, or commands whose executable cannot be verified.
    It never treats the entire command line (including arguments) as a path.
    """
    if command_string is None:
        return None

    raw = str(command_string).strip()
    if not raw or raw.casefold() in _PLACEHOLDER_COMMANDS:
        return None

    # Remove a single pair of wrapper quotes only when it encloses the whole
    # command. A command beginning with a quoted EXE is handled separately.
    raw = raw.strip()

    # 1) Quoted executable: "C:\\Program Files\\App\\app.exe" --arg
    quoted = re.match(r'^\s*["\']([^"\']+)["\']', raw)
    if quoted:
        found = _existing_file(quoted.group(1), base_dir)
        if found:
            return found

    # 2) Find executable/script/DLL suffixes in order. This solves unquoted
    # paths containing spaces without accidentally including trailing args.
    # Example: C:\\Program Files\\Vendor App\\app.exe -silent
    for match in _EXECUTABLE_SUFFIX_RE.finditer(raw):
        candidate = raw[:match.end()]
        candidate = candidate.strip().strip('"').strip("'")
        found = _existing_file(candidate, base_dir)
        if found:
            return found

    # 3) If no known extension was present, test the whole string as a path.
    found = _existing_file(raw, base_dir)
    if found:
        return found

    # 4) Parse standard Windows argv and progressively join leading tokens.
    # Joining matters for unquoted paths such as C:\\Program Files\\app.exe.
    tokens = _windows_command_line_tokens(raw)
    for index, token in enumerate(tokens):
        token = token.strip().strip('"').strip("'")
        if not token:
            continue

        # A switch marks the end of a conventional unquoted executable path.
        # Do not confuse /c with an absolute path beginning with C:\\.
        if token.startswith(("-", "/")) and not re.match(r"^[A-Za-z]:[\\/]", token):
            break

        candidate = " ".join(tokens[: index + 1]).strip().strip('"').strip("'")
        found = _existing_file(candidate, base_dir)
        if found:
            return found

    return None


@dataclass
class AutostartEntry:
    """Compatible entry model with safe path resolution helpers."""

    name: str
    command: str
    source_type: str
    source_path: str
    enabled: bool = True
    icon_path: Optional[str] = None

    def get_clean_executable_path(self) -> Optional[str]:
        """Return the verified target file, including for Startup .lnk files."""
        source = (self.source_path or "").strip().strip('"')

        # For Startup-folder shortcuts the source_path is the .lnk itself.
        # Resolve TargetPath rather than trying to extract an icon from .lnk.
        if source.lower().endswith(".lnk") and os.path.isfile(source):
            try:
                from .icon_extractor import resolve_shortcut  # type: ignore
            except ImportError:
                try:
                    from icon_extractor import resolve_shortcut  # type: ignore
                except ImportError:
                    resolve_shortcut = None  # type: ignore

            if resolve_shortcut is not None:
                info = resolve_shortcut(source)
                if info and info.target_path:
                    target = _existing_file(info.target_path, info.working_directory or None)
                    if target:
                        return target

        # For registry/task entries, parse the command. A missing executable
        # becomes None, which intentionally triggers the visual placeholder.
        return extract_executable_path(self.command)

    def get_icon_lookup_path(self) -> Optional[str]:
        """Return the best input for IconExtractor.get_icon().

        Keep a .lnk as the icon source when possible: IconExtractor can then
        honor its custom IconLocation before falling back to TargetPath.
        """
        if self.icon_path and os.path.exists(self.icon_path):
            return self.icon_path

        source = (self.source_path or "").strip().strip('"')
        if source.lower().endswith(".lnk") and os.path.isfile(source):
            return source

        return self.get_clean_executable_path()
