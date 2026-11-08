from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from models import AutostartEntry


DARK_PANEL = "#191D24"
TEXT_MUTED = "#929BA8"


def middle_ellipsis(value: str, max_chars: int = 92) -> str:
    value = value or ""
    if len(value) <= max_chars:
        return value
    keep = max(10, (max_chars - 3) // 2)
    return value[:keep] + "..." + value[-keep:]


class Tooltip:
    def __init__(self, widget, text: str, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: Optional[str] = None
        self._window: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._window or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            window = tk.Toplevel(self.widget)
            window.wm_overrideredirect(True)
            window.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                window,
                text=self.text,
                justify="left",
                background="#252B35",
                foreground="#E8EDF4",
                relief="solid",
                borderwidth=1,
                padx=8,
                pady=5,
                font=("Segoe UI", 9),
                wraplength=720,
            )
            label.pack()
            self._window = window
        except Exception:
            self._window = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None


class AutostartRow(ctk.CTkFrame):
    def __init__(
        self,
        master,
        entry: AutostartEntry,
        on_toggle: Callable[[AutostartEntry, bool, "AutostartRow"], None],
        on_delete: Callable[[AutostartEntry], None],
    ) -> None:
        super().__init__(master, fg_color=DARK_PANEL, corner_radius=8, border_width=1, border_color="#252B35")
        self.entry = entry
        self.on_toggle = on_toggle
        self.on_delete = on_delete
        self._image_ref = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.icon_label = ctk.CTkLabel(self, text="", width=42, height=42)
        self.icon_label.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10, sticky="n")
        self._load_icon(entry.icon_path)

        self.name_label = ctk.CTkLabel(
            self,
            text=entry.name,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color="#E8EDF4",
            anchor="w",
        )
        self.name_label.grid(row=0, column=1, padx=(0, 10), pady=(10, 1), sticky="ew")

        location = entry.executable_path or entry.command or entry.source_path
        self.path_label = ctk.CTkLabel(
            self,
            text=middle_ellipsis(location, 100),
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.path_label.grid(row=1, column=1, padx=(0, 10), pady=(0, 2), sticky="ew")
        Tooltip(self.path_label, f"Команда: {entry.command}\n\nИсточник: {entry.source_path}")

        if entry.source_type == "registry":
            source_name = f"Реестр · {entry.registry_hive or 'Windows'}"
        elif entry.source_type == "folder":
            scope = "общая папка" if entry.source_scope == "common" else "папка пользователя"
            source_name = f"Автозагрузка · {scope}"
        elif entry.source_type == "task":
            source_name = "Планировщик задач"
        else:
            source_name = entry.source_type
        if entry.is_run_once:
            source_name += " · RunOnce"
        self.source_label = ctk.CTkLabel(
            self,
            text=source_name,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color="#748092",
            anchor="w",
        )
        self.source_label.grid(row=2, column=1, padx=(0, 10), pady=(0, 10), sticky="ew")

        self.switch = ctk.CTkSwitch(
            self,
            text="",
            width=42,
            switch_width=36,
            switch_height=20,
            onvalue=1,
            offvalue=0,
            command=self._switch_changed,
        )
        self.switch.grid(row=0, column=2, rowspan=3, padx=(8, 8), pady=10)
        self.switch.select() if entry.enabled else self.switch.deselect()
        if not entry.can_toggle:
            self.switch.configure(state="disabled")
            Tooltip(self.switch, entry.disabled_reason or "Изменение недоступно.")

        self.delete_button = ctk.CTkButton(
            self,
            text="×",
            width=30,
            height=30,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#3B252A",
            text_color="#D58A91",
            font=ctk.CTkFont(family="Segoe UI", size=21),
            command=lambda: self.on_delete(self.entry),
        )
        self.delete_button.grid(row=0, column=3, rowspan=3, padx=(0, 10), pady=10)
        if not entry.can_delete:
            self.delete_button.configure(state="disabled")
            Tooltip(self.delete_button, entry.disabled_reason or "Удаление недоступно.")

        self._search_blob = " ".join(
            (
                entry.name,
                entry.command,
                entry.source_path,
                entry.executable_path or "",
            )
        ).casefold()

    def _load_icon(self, path: Optional[str]) -> None:
        try:
            if path and Path(path).is_file():
                with Image.open(path) as img:
                    image = img.convert("RGBA").resize((32, 32), Image.Resampling.LANCZOS)
            else:
                image = Image.new("RGBA", (32, 32), (65, 73, 86, 255))
            self._image_ref = ctk.CTkImage(light_image=image, dark_image=image, size=(32, 32))
            self.icon_label.configure(image=self._image_ref)
        except Exception:
            self.icon_label.configure(text="▣", text_color=TEXT_MUTED)

    def _switch_changed(self) -> None:
        self.on_toggle(self.entry, bool(self.switch.get()), self)

    def set_busy(self, busy: bool) -> None:
        if self.entry.can_toggle:
            self.switch.configure(state="disabled" if busy else "normal")
        if self.entry.can_delete:
            self.delete_button.configure(state="disabled" if busy else "normal")

    def restore_switch(self) -> None:
        self.switch.select() if self.entry.enabled else self.switch.deselect()
