from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable

import customtkinter as ctk

from autostart_manager import AutostartManager
from models import AutostartEntry
from ui.widgets import AutostartRow, Tooltip

import os

# Получаем путь к папке ui (где лежит app.py)
current_dir = os.path.dirname(os.path.abspath(__file__))

# Поднимаемся на уровень выше и берем иконку
icon_path = os.path.join(current_dir, "..", "icon.ico")


BG = "#101319"
PANEL = "#171B22"
TEXT = "#E8EDF4"
MUTED = "#929BA8"
ACCENT = "#477BC4"


class AutostartApp(ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__()

        self.manager = AutostartManager()
        self.entries: list[AutostartEntry] = []
        self.rows: list[AutostartRow] = []
        self.messages: queue.Queue = queue.Queue()
        self._loading = False
        self._busy = False
        self._closing = False

        
        self.title("AutoRunPro")
        self.iconbitmap(icon_path) 
        self.geometry("1060x760")
        self.minsize(780, 540)
        self.configure(fg_color=BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_list()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_messages)
        self.refresh_list()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=(24, 12), pady=(18, 16))
        ctk.CTkLabel(
            left,
            text="Автозагрузка",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        self.count_label = ctk.CTkLabel(
            left,
            text="Сканирование источников…",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=MUTED,
        )
        self.count_label.pack(anchor="w", pady=(3, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=(8, 22), pady=18)
        self.admin_label = ctk.CTkLabel(
            right,
            text="Администратор" if self.manager.is_admin else "Обычный режим",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color="#8CA6C6" if self.manager.is_admin else MUTED,
        )
        self.admin_label.pack(side="left", padx=(0, 14))

        self.refresh_button = ctk.CTkButton(
            right,
            text="Обновить",
            width=94,
            height=34,
            corner_radius=7,
            fg_color="#2A303A",
            hover_color="#343D4A",
            text_color=TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self.refresh_list,
        )
        self.refresh_button.pack(side="left", padx=(0, 8))
        self.add_button = ctk.CTkButton(
            right,
            text="+ Добавить",
            width=112,
            height=34,
            corner_radius=7,
            fg_color=ACCENT,
            hover_color="#588BD2",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self.add_executable,
        )
        self.add_button.pack(side="left")

    def _build_list(self) -> None:
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=22, pady=(16, 10))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        search_line = ctk.CTkFrame(main, fg_color="transparent")
        search_line.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        search_line.grid_columnconfigure(0, weight=1)
        self.search_entry = ctk.CTkEntry(
            search_line,
            placeholder_text="Поиск по названию, пути или источнику",
            height=36,
            corner_radius=7,
            fg_color=PANEL,
            border_color="#2A303A",
            text_color=TEXT,
            placeholder_text_color="#758091",
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<KeyRelease>", lambda _event: self._filter_rows())

        self.scroll_frame = ctk.CTkScrollableFrame(
            main,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#3A424E",
            scrollbar_button_hover_color="#566273",
        )
        self.scroll_frame.grid(row=1, column=0, sticky="nsew")
        self.scroll_frame.grid_columnconfigure(0, weight=1)
        self.empty_label = ctk.CTkLabel(
            self.scroll_frame,
            text="Записи автозагрузки не найдены",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=MUTED,
        )

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=34)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(
            footer,
            text="Готово",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=MUTED,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew", padx=22, pady=8)

    def refresh_list(self) -> None:
        if self._loading or self._busy or self._closing:
            return
        self._loading = True
        self.refresh_button.configure(state="disabled")
        self.add_button.configure(state="disabled")
        self.status_label.configure(text="Сканирование реестра, папок и планировщика…")

        def worker() -> None:
            try:
                entries = self.manager.scan_all()
                warnings = list(self.manager.last_scan_warnings)
                self.messages.put(("scan_ok", entries, warnings))
            except Exception as exc:
                self.messages.put(("scan_error", str(exc)))

        threading.Thread(target=worker, name="AutostartScan", daemon=True).start()

    def _process_messages(self) -> None:
        if self._closing:
            return
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "scan_ok":
                    _, entries, warnings = message
                    self.entries = entries
                    self._render_entries()
                    self._loading = False
                    self.refresh_button.configure(state="normal")
                    self.add_button.configure(state="normal")
                    enabled_count = sum(1 for item in entries if item.enabled)
                    self.count_label.configure(
                        text=f"{len(entries)} записей · {enabled_count} включено · {len(entries) - enabled_count} отключено"
                    )
                    status = "Список обновлён"
                    if warnings:
                        status += f" · предупреждений: {len(warnings)}"
                        Tooltip(self.status_label, "\n".join(warnings))
                    if not self.manager.is_admin:
                        status += " · системные записи доступны только для чтения"
                    self.status_label.configure(text=status)
                elif kind == "scan_error":
                    _, error = message
                    self._loading = False
                    self.refresh_button.configure(state="normal")
                    self.add_button.configure(state="normal")
                    self.status_label.configure(text="Не удалось выполнить сканирование")
                    messagebox.showerror("Ошибка сканирования", error, parent=self)
                elif kind == "action_ok":
                    _, success_text = message
                    self._busy = False
                    self.status_label.configure(text=success_text)
                    self.refresh_list()
                elif kind == "action_error":
                    _, error = message
                    self._busy = False
                    self.status_label.configure(text="Операция не выполнена")
                    messagebox.showerror("Не удалось изменить автозагрузку", error, parent=self)
                    self.refresh_list()
        except queue.Empty:
            pass
        self.after(100, self._process_messages)

    def _render_entries(self) -> None:
        for row in self.rows:
            row.destroy()
        self.rows.clear()
        for index, entry in enumerate(self.entries):
            row = AutostartRow(
                self.scroll_frame,
                entry,
                on_toggle=self._on_toggle,
                on_delete=self._on_delete,
            )
            row.grid(row=index, column=0, sticky="ew", padx=(2, 8), pady=(0, 7))
            self.rows.append(row)
        self._filter_rows()

    def _filter_rows(self) -> None:
        query = self.search_entry.get().strip().casefold()
        visible = 0
        for row in self.rows:
            match = not query or query in row._search_blob
            if match:
                row.grid()
                visible += 1
            else:
                row.grid_remove()
        if visible == 0:
            self.empty_label.configure(
                text="Ничего не найдено" if query else "Записи автозагрузки не найдены"
            )
            self.empty_label.grid(row=len(self.rows) + 1, column=0, pady=32)
        else:
            self.empty_label.grid_remove()

    def _run_action(self, action: Callable[[], None], success_text: str, row: AutostartRow | None = None) -> None:
        if self._busy or self._loading:
            return
        self._busy = True
        self.refresh_button.configure(state="disabled")
        self.add_button.configure(state="disabled")
        for item in self.rows:
            item.set_busy(True)
        self.status_label.configure(text="Выполняется операция…")

        def worker() -> None:
            try:
                action()
                self.messages.put(("action_ok", success_text))
            except Exception as exc:
                self.messages.put(("action_error", str(exc)))

        threading.Thread(target=worker, name="AutostartAction", daemon=True).start()

    def _on_toggle(self, entry: AutostartEntry, enabled: bool, row: AutostartRow) -> None:
        if not entry.can_toggle:
            row.restore_switch()
            messagebox.showwarning(
                "Недостаточно прав",
                entry.disabled_reason or "Изменение этой записи недоступно.",
                parent=self,
            )
            return
        action = lambda: self.manager.set_enabled(entry, enabled)
        verb = "включена" if enabled else "отключена"
        self._run_action(action, f"«{entry.name}» {verb}", row)

    def _on_delete(self, entry: AutostartEntry) -> None:
        if not entry.can_delete:
            messagebox.showwarning(
                "Недостаточно прав",
                entry.disabled_reason or "Удаление этой записи недоступно.",
                parent=self,
            )
            return
        details = f"Удалить запись «{entry.name}» из автозагрузки?\n\nИсточник: {entry.source_path}"
        if entry.is_system_item:
            details += (
                "\n\nВнимание: это системная или общая запись. Её удаление может повлиять "
                "на работу Windows или других пользователей компьютера."
            )
        if not messagebox.askyesno("Подтверждение удаления", details, icon="warning", parent=self):
            return
        self._run_action(lambda: self.manager.remove(entry), f"«{entry.name}» удалена")

    def add_executable(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Выберите программу для автозагрузки",
            filetypes=[("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")],
        )
        if not path:
            return

        def action() -> None:
            self.manager.add_executable(path)

        self._run_action(action, f"Добавлена: {path}")

    def _on_close(self) -> None:
        self._closing = True
        self.destroy()
