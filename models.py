from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class AutostartEntry:
    """Normalized startup item gathered from one of the supported Windows sources."""

    name: str
    command: str
    source_type: str
    source_path: str
    enabled: bool = True
    icon_path: Optional[str] = None

    # Resolved executable (when the command contains one). Kept separately from
    # command so arguments are never lost when displaying or changing an item.
    executable_path: Optional[str] = None

    # Registry metadata.
    registry_hive: Optional[str] = None
    registry_subkey: Optional[str] = None
    registry_value_name: Optional[str] = None
    registry_view: str = "native"
    registry_value_type: Optional[int] = None
    is_run_once: bool = False
    run_once_stashed: bool = False
    backup_subkey: Optional[str] = None

    # Startup-folder metadata.
    folder_path: Optional[str] = None
    source_scope: Optional[str] = None  # "user" or "common"

    # Scheduled-task metadata.
    task_path: Optional[str] = None

    # StartupApproved metadata. Values are intentionally explicit so that
    # UI code never has to infer registry locations.
    approval_hive: Optional[str] = None
    approval_subkey: Optional[str] = None
    approval_value_name: Optional[str] = None
    approval_view: str = "native"

    # Capabilities are filled by AutostartManager after scanning.
    can_toggle: bool = True
    can_delete: bool = True
    disabled_reason: Optional[str] = None

    @property
    def is_system_item(self) -> bool:
        source = (self.source_path or "").lower()
        command = (self.executable_path or self.command or "").lower()
        task = (self.task_path or "").lower()
        return (
            self.registry_hive == "HKLM"
            or self.source_scope == "common"
            or "\\windows\\system32\\" in command
            or task.startswith("\\microsoft\\windows\\")
            or "\\windows\\system32\\" in source
        )
