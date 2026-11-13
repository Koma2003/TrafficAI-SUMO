"""Inspect and optimise the selected SUMO model without touching its originals.

Only the Tk thread accesses widgets. Backend work sends progress and results
through a queue; each application owns one dialog and one active job.
"""
from __future__ import annotations

import math
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4
from ui.project_paths import model_file


def parse_parameters(values: dict[str, str]) -> dict:
    """Validate form values without substituting backend timing constraints."""
    result = {}
    for key, label in [("green", "Зелёный"), ("yellow", "Жёлтый"),
                       ("red", "Непрерывный красный направления"), ("all_red", "Всем красный")]:
        try:
            lo, hi = (float(values[f"{key}_{part}"].strip().replace(",", ".")) for part in ("min", "max"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}: введите числовые границы в секундах") from exc
        if not all(math.isfinite(number) for number in (lo, hi)):
            raise ValueError(f"{label}: границы должны быть конечными числами")
        if lo < 0 or hi < lo or (key != "all_red" and lo == 0):
            rule = "0 ≤ минимум ≤ максимум" if key == "all_red" else "0 < минимум ≤ максимум"
            raise ValueError(f"{label}: требуется {rule}")
        result[f"{key}_min"], result[f"{key}_max"] = lo, hi
    cycle = values.get("cycle", "").strip()
    try:
        result["cycle"] = float(cycle.replace(",", ".")) if cycle else None
    except ValueError as exc:
        raise ValueError("Цикл: введите число секунд или оставьте поле пустым") from exc
    if result["cycle"] is not None and (not math.isfinite(result["cycle"]) or result["cycle"] <= 0):
        raise ValueError("Длительность цикла должна быть положительным конечным числом")
    for key, label, minimum in [("generations", "Поколений", 1),
                                 ("population", "Вариантов в поколении", 2), ("seed", "Случайное зерно", 0)]:
        try:
            number = int(values[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}: требуется целое число") from exc
        if number < minimum:
            raise ValueError(f"{label}: значение должно быть не меньше {minimum}")
        result[key] = number
    return result


def open_signal_plan_dialog(parent: tk.Misc, project_root: str | Path) -> tk.Toplevel:
    """Reopen the same form, retaining its active-job guard and cancellation."""
    existing = getattr(parent, "_signal_plan_dialog", None)
    if existing is not None and existing.window.winfo_exists():
        existing.window.deiconify()
        existing.window.lift()
        return existing.window
    dialog = _SignalPlanDialog(parent, Path(project_root).resolve())
    parent._signal_plan_dialog = dialog
    return dialog.window


class _SignalPlanDialog:
    def __init__(self, parent, project_root):
        from ui.appearance import configure_ui, size_window
        configure_ui(parent)
        self.project_root = project_root
        self.window = tk.Toplevel(parent)
        self.window.title("Светофорные планы текущей модели")
        size_window(self.window, 1460, 1100)
        self.window.minsize(980, 700)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.model = self.loaded_config = self.result = None
        self.editable = []
        self.config_text = tk.StringVar(value=str(model_file(project_root, "config")))
        self.status_text = tk.StringVar(value="Выберите конфигурацию SUMO и загрузите её фазы.")
        self.model_text = tk.StringVar(value="Модель ещё не проверена.")
        defaults = {"green_min": "15", "green_max": "60", "yellow_min": "3", "yellow_max": "5",
                    "red_min": "10", "red_max": "90", "all_red_min": "1", "all_red_max": "3",
                    "cycle": "", "generations": "3", "population": "8", "seed": "42"}
        self.values = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        self._build()
        self.window.after(100, self._poll)
        self.window.after_idle(self._inspect)

    def _entry(self, parent, variable, **kwargs):
        entry = ttk.Entry(parent, textvariable=variable, **kwargs)
        self.editable.append(entry)
        return entry

    def _build(self):
        viewport = tk.Canvas(self.window, highlightthickness=0, background='#f3f5f7')
        scrollbar = ttk.Scrollbar(self.window, orient='vertical', command=viewport.yview)
        scrollbar.pack(side='right', fill='y')
        viewport.pack(side='left', fill='both', expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        container = ttk.Frame(viewport, padding=16)
        content = viewport.create_window((0,0),window=container,anchor='nw')
        container.bind('<Configure>',lambda event: viewport.configure(scrollregion=viewport.bbox('all')))
        viewport.bind('<Configure>',lambda event: viewport.itemconfigure(content,width=event.width))
        self.window.bind('<MouseWheel>',lambda event: viewport.yview_scroll(-int(event.delta/120),'units'))
        ttk.Label(container, text="Настройки применяются к выбранной модели SUMO.",
                  font=("Segoe UI", -21, "bold")).pack(anchor="w", pady=(0, 8))
        ttk.Label(container, text="Метод: генетический подбор статического плана. RL остаётся отдельным экспериментом.",
                  wraplength=1320).pack(anchor="w", pady=(0, 8))
        source = ttk.Frame(container)
        source.pack(fill="x")
        ttk.Label(source, text="Конфигурация:").pack(side="left", padx=(0, 7))
        self._entry(source, self.config_text).pack(side="left", fill="x", expand=True)
        browse = ttk.Button(source, text="Выбрать…", command=self._browse)
        browse.pack(side="left", padx=6)
        self.editable.append(browse)
        self.inspect_button = ttk.Button(source, text="Загрузить фазы", command=self._inspect)
        self.inspect_button.pack(side="left")
        ttk.Label(container, textvariable=self.model_text, wraplength=1320,
                  justify="left").pack(fill="x", pady=8)
        frame = ttk.Frame(container)
        frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(frame, columns=("tls", "program", "phase", "duration", "kind", "state"),
                                  show="headings", height=4)
        for key, label, width in [("tls", "Объект", 100), ("program", "Программа", 85), ("phase", "Фаза", 55),
                                  ("duration", "Длительность, с", 110), ("kind", "Тип", 120), ("state", "Состояние", 330)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="w", stretch=key in {"state", "tls"})
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.prepare_button = ttk.Button(container, text="Создать регулируемый вариант",
                                         command=self._prepare, state="disabled")
        self.prepare_button.pack(anchor="w", pady=(7, 10))
        bounds = ttk.LabelFrame(container, text="Допустимые длительности в секундах", padding=9)
        bounds.pack(fill="x")
        for column, text in enumerate(["Интервал", "Минимум", "Максимум"]):
            ttk.Label(bounds, text=text).grid(row=0, column=column, padx=(0, 12), sticky="w")
        for row, key, label in [(1, "green", "Зелёный"), (2, "yellow", "Жёлтый"),
                                (3, "red", "Непрерывный красный направления"),
                                (4, "all_red", "Отдельная фаза «всем красный»")]:
            ttk.Label(bounds, text=label).grid(row=row, column=0, sticky="w", pady=3)
            for column, suffix in [(1, "min"), (2, "max")]:
                self._entry(bounds, self.values[f"{key}_{suffix}"], width=8).grid(row=row, column=column, padx=12, pady=3)
        ttk.Label(bounds, text="Красный направления учитывается целиком, в том числе через границу цикла.\n"
                              "Пешеходные и защитные интервалы сохраняются; несовместимые настройки будут отклонены.",
                  wraplength=1250, justify="left").grid(row=5, column=0, columnspan=3, sticky="w", pady=(7, 0))
        options = ttk.Frame(container)
        options.pack(fill="x", pady=9)
        for column, key, label in [(0, "cycle", "Цикл, с"), (2, "generations", "Поколений"),
                                   (4, "population", "Вариантов"), (6, "seed", "Зерно")]:
            ttk.Label(options, text=label).grid(row=0, column=column, padx=(0, 5))
            self._entry(options, self.values[key], width=7).grid(row=0, column=column + 1, padx=(0, 14))
        ttk.Label(container, text="Пустой цикл — сохранить исходный цикл каждого светофора. "
                                 "Исходные файлы не перезаписываются.", wraplength=1320).pack(anchor="w")
        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(9, 5))
        self.optimise_button = ttk.Button(buttons, text="Подобрать план", command=self._optimise, state="disabled")
        self.optimise_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Отменить расчёт", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.report_button = ttk.Button(buttons, text="Открыть отчёт", command=self._open_report, state="disabled")
        self.report_button.pack(side="left", padx=8)
        self.sumo_button = ttk.Button(buttons, text="Результат в SUMO", command=self._open_sumo, state="disabled")
        self.sumo_button.pack(side="left")
        self.progress = ttk.Progressbar(container, mode="indeterminate")
        self.progress.pack(fill="x", pady=5)
        ttk.Label(container, textvariable=self.status_text, wraplength=1320, justify="left").pack(fill="x")

    def _path(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.project_root / path).resolve()

    def _selected_config(self):
        if not self.config_text.get().strip():
            raise ValueError("Выберите конфигурацию SUMO")
        config = self._path(self.config_text.get().strip())
        if not config.is_file():
            raise ValueError(f"Конфигурация не найдена: {config}")
        return config

    def _browse(self):
        chosen = filedialog.askopenfilename(parent=self.window, title="Выберите конфигурацию SUMO",
                    initialdir=str(self.project_root / "sumo"),
                    filetypes=[("Конфигурации SUMO", "*.sumocfg *.sumo.cfg"), ("Все файлы", "*.*")])
        if chosen:
            self.config_text.set(chosen)
            self._inspect()

    def _set_controls(self):
        for widget in self.editable + [self.inspect_button]:
            widget.configure(state="disabled" if self.busy else "normal")
        has_signals = bool(self.model and self.model.get("has_signals"))
        self.prepare_button.configure(state="normal" if self.model and not has_signals and not self.busy else "disabled")
        self.optimise_button.configure(state="normal" if has_signals and not self.busy else "disabled")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")
        for button in (self.report_button, self.sumo_button):
            button.configure(state="normal" if self.result and not self.busy else "disabled")

    def _begin(self, kind, work):
        if self.busy:
            return
        self.busy = True
        self.cancel = threading.Event()
        self._set_controls()
        self.progress.start(15)
        self.status_text.set({"inspect": "Читаю выбранную модель…", "prepare": "Создаю отдельный регулируемый вариант…",
                              "optimise": "Подбираю и проверяю план…"}[kind])
        event = self.cancel
        def progress(*items, **details):
            text = " — ".join([str(item) for item in items if item is not None] +
                              [f"{key}: {value}" for key, value in details.items()])
            self.messages.put(("progress", text or "Расчёт…"))
        def worker():
            try:
                self.messages.put(("complete", kind, work(event, progress), event.is_set()))
            except Exception as exc:
                self.messages.put(("error", kind, str(exc), event.is_set()))
        threading.Thread(target=worker, daemon=True, name="TrafficAI-signal-plan").start()

    def _inspect(self):
        if self.busy:
            return
        self.model = self.loaded_config = self.result = None
        self.table.delete(*self.table.get_children())
        try:
            config = self._selected_config()
        except ValueError as exc:
            self.model_text.set(str(exc))
            self.status_text.set("Выберите существующий .sumocfg, затем загрузите фазы.")
            self._set_controls()
            return
        self.model_text.set(f"Загружаю модель: {config}")
        def inspect(event, progress):
            from sumo.signal_plan import inspect_model
            return {"config": config, "model": inspect_model(config)}
        self._begin("inspect", inspect)

    def _show_model(self, config, model):
        self.loaded_config, self.model = config, model
        self.config_text.set(str(config))
        self.table.delete(*self.table.get_children())
        programs = model.get("traffic_lights", [])
        for signal in programs:
            for index, phase in enumerate(signal.get("phases", [])):
                self.table.insert("", "end", values=(signal.get("id", ""), signal.get("program_id", ""), index,
                                  phase.get("duration", ""), phase.get("kind", ""), phase.get("state", "")))
        if model.get("has_signals"):
            cycles = sorted({float(signal["cycle"]) for signal in programs})
            self.values["cycle"].set(f"{cycles[0]:g}" if len(cycles) == 1 else "")
            self.model_text.set(f"Сеть: {model.get('net_file', '')}\nСветофорных программ: {len(programs)}. "
                                f"Циклы: {', '.join(f'{value:g} с' for value in cycles)}.")
            self.status_text.set("Фазы загружены. Задайте диапазоны и запустите подбор.")
        else:
            self.values["cycle"].set("")
            self.model_text.set("В сохранённой модели нет светофорной программы: перекрёсток смоделирован как priority. "
                                "Это свойство файла SUMO и не вывод о реальном перекрёстке.\n"
                                "Можно создать отдельный регулируемый вариант; исходная модель сохранится.")
            self.status_text.set("Для подбора нужен регулируемый вариант модели.")

    def _new_output(self, prefix):
        return self.project_root / "runs" / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"

    def _prepare(self):
        if self.busy:
            return
        try:
            config = self._selected_config()
            if self.loaded_config != config:
                raise ValueError("Сначала загрузите выбранную конфигурацию")
        except ValueError as exc:
            messagebox.showerror("Модель SUMO", str(exc), parent=self.window)
            return
        output = self._new_output("controlled_model")
        def prepare(event, progress):
            from sumo.signal_plan import inspect_model, prepare_controlled_model
            new_config = self._path(prepare_controlled_model(config, output))
            return {"config": new_config, "model": inspect_model(new_config)}
        self._begin("prepare", prepare)

    def _optimise(self):
        if self.busy:
            return
        try:
            config = self._selected_config()
            if self.loaded_config != config or not self.model or not self.model.get("has_signals"):
                raise ValueError("Сначала загрузите фазы выбранной конфигурации")
            parameters = parse_parameters({key: variable.get() for key, variable in self.values.items()})
        except ValueError as exc:
            messagebox.showerror("Параметры плана", str(exc), parent=self.window)
            return
        output = self._new_output("signal_plan")
        self.result = None
        def optimise(event, progress):
            from sumo.signal_plan import optimize_model
            return optimize_model(config, output, **parameters, progress=progress, cancel=event, backend="auto")
        self._begin("optimise", optimise)

    def _poll(self):
        if not self.window.winfo_exists():
            self.cancel.set()
            return
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == "progress":
                    self.status_text.set(message[1])
                    continue
                _, kind, payload, cancelled = message
                self.busy = False
                self.progress.stop()
                if cancelled:
                    self.status_text.set("Расчёт отменён. Исходная модель сохранена; неполный результат не выбран для просмотра.")
                elif message[0] == "error":
                    self.status_text.set(f"Не удалось завершить операцию: {payload}")
                    if kind == "inspect":
                        self.model_text.set("Модель не загружена. Проверьте выбранную конфигурацию.")
                    if self.window.state() != "withdrawn":
                        messagebox.showerror("Светофорный план", payload, parent=self.window)
                elif kind in {"inspect", "prepare"}:
                    self._show_model(self._path(payload["config"]), payload["model"])
                    if kind == "prepare":
                        self.status_text.set("Создан отдельный регулируемый вариант. Проверьте его фазы и задайте ограничения.")
                else:
                    self.result = payload
                    location = Path(payload.get("config_file", payload.get("report_file", ""))).parent
                    self.status_text.set(f"Готово. План и сравнение сохранены отдельно: {location}")
                self._set_controls()
        except queue.Empty:
            pass
        self.window.after(100, self._poll)

    def _cancel(self):
        if self.busy:
            self.cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status_text.set("Отменяю расчёт после текущего шага…")

    def _open_report(self):
        if not self.result:
            return
        report = self._path(self.result["report_file"])
        if not report.is_file():
            messagebox.showerror("Отчёт", f"Файл не найден: {report}", parent=self.window)
            return
        webbrowser.open(report.as_uri())

    def _open_sumo(self):
        if not self.result:
            return
        config = self._path(self.result["config_file"])
        if not config.is_file():
            messagebox.showerror("SUMO", f"Конфигурация не найдена: {config}", parent=self.window)
            return
        try:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            subprocess.Popen(["sumo-gui", "-c", str(config)], cwd=config.parent, **options)
        except OSError as exc:
            messagebox.showerror("SUMO", str(exc), parent=self.window)

    def _close(self):
        if self.busy:
            self._cancel()
        # Retain singleton state while hidden, preventing duplicate workers.
        self.window.withdraw()
