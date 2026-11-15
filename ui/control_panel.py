#========================================================================
# ui/control_panel.py — единая панель управления TrafficAI.
#
# - встроенная карта (tkintermapview)
# - выбор перекрёстка: ПКМ на карте ИЛИ браузер (поиск по городу)
# - кнопки: пересобрать всё, открыть SUMO, редактор полос
# - живой лог всех шагов (geometry_builder → net_builder → route_builder)
#
# ИСПРАВЛЕНО:
#   - TILE_SERVER: tile.openstreetmap.org блокирует User-Agent tkintermapview
#     (403 Access blocked), поэтому тайлы берутся с другого сервера;
#   - убран лишний "\" в f-строке лога (SyntaxWarning).
#========================================================================

import os, sys, json, queue, threading, subprocess
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import scrolledtext
from ui.project_paths import model_file

try:
    import tkintermapview
except ImportError:
    tkintermapview = None

PROJECT_ROOT = Path(__file__).parent.parent
GEOMETRY_FILE = PROJECT_ROOT / "osm" / "geometry.json"
COORDS_FILE = PROJECT_ROOT / "clicked_coords.json"
CONFIG_FILE = PROJECT_ROOT / "sumo" / "intersection.sumocfg"

# Сервер тайлов для встроенной карты. Если карта не грузится — поменяйте здесь.
TILE_SERVER = "https://mt0.google.com/vt/lyrs=m&hl=ru&x={x}&y={y}&z={z}"
TILE_MAX_ZOOM = 20

COLOR_IN = "#22c55e"    # зелёный — въезд
COLOR_OUT = "#3b82f6"   # синий — выезд
COLOR_SEL = "#ef4444"   # красный — выбранный подход


class ControlPanel:
    def __init__(self, root):
        from ui.appearance import configure_ui, size_window
        configure_ui(root)
        self.root = root
        self.root.title("TrafficAI")
        size_window(root, 1500, 960)

        self.log_queue = queue.Queue()
        self.approach_lines = []        # [(approach_dict, map_path_item)]
        self.approaches_by_iid = {}
        self._highlight_line = None
        self._current_proc = None

        self._build_ui()
        self._drain_log_forever()
        self.refresh_from_disk()

    # ---------- UI ----------

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=5)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        primary = toolbar
        secondary = toolbar
        from ui.area_dialog import open_area_dialog
        from ui.signal_plan_dialog import open_signal_plan_dialog
        from ui.signal_dialog import open_signal_dialog
        ttk.Button(primary, text="Район и отчёт", command=lambda: open_area_dialog(
            self.root, self.map_widget, PROJECT_ROOT)).pack(side=tk.LEFT, padx=2)
        ttk.Button(primary, text="Светофорные планы", command=lambda: open_signal_plan_dialog(
            self.root, PROJECT_ROOT)).pack(side=tk.LEFT, padx=2)
        self.btn_sumo = ttk.Button(primary, text="Открыть SUMO", command=self.on_open_sumo)
        self.btn_sumo.pack(side=tk.LEFT, padx=2)
        self.status = ttk.Label(primary, text="Готов", foreground="gray")
        self.status.pack(side=tk.RIGHT, padx=12)
        self.btn_choose = ttk.Button(secondary, text="Выбрать перекрёсток", command=self.on_choose_browser)
        self.btn_choose.pack(side=tk.LEFT, padx=2)
        self.btn_rebuild = ttk.Button(secondary, text="Пересобрать сеть", command=self.on_rebuild)
        self.btn_rebuild.pack(side=tk.LEFT, padx=2)
        self.btn_editor = ttk.Button(secondary, text="Редактор полос", command=self.on_open_editor)
        self.btn_editor.pack(side=tk.LEFT, padx=2)
        ttk.Button(secondary, text="RL-обучение", command=lambda: open_signal_dialog(
            self.root, PROJECT_ROOT)).pack(side=tk.LEFT, padx=2)
        self.btn_cancel = ttk.Button(secondary, text="Отмена", command=self.on_cancel, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=2)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        map_frame = ttk.Frame(paned)
        paned.add(map_frame, weight=3)

        if tkintermapview is None:
            ttk.Label(
                map_frame,
                text="tkintermapview не установлен.\n\nВ терминале:  pip install tkintermapview",
                font=("Arial", 12), foreground="red"
            ).pack(expand=True)
            self.map_widget = None
        else:
            self.map_widget = tkintermapview.TkinterMapView(map_frame, corner_radius=0)
            # ИСПРАВЛЕНО: сервер тайлов по умолчанию (OSM) отдаёт 403
            self.map_widget.set_tile_server(TILE_SERVER, max_zoom=TILE_MAX_ZOOM)
            self.map_widget.pack(fill=tk.BOTH, expand=True)
            self.map_widget.set_position(55.751244, 37.618423)
            self.map_widget.set_zoom(15)
            # ПКМ: прямо на карте выбрать перекрёсток
            self.map_widget.add_right_click_menu_command(
                label="🎯 Сделать перекрёстком",
                command=self._on_map_pick,
                pass_coords=True,
            )

        info = ttk.Frame(paned, width=540)
        paned.add(info, weight=1)

        ttk.Label(info, text="Перекрёсток", font=("Segoe UI", -20, "bold")).pack(anchor=tk.W, pady=(0, 4))
        self.info_label = ttk.Label(info, text="Не выбран", justify=tk.LEFT, wraplength=400)
        self.info_label.pack(anchor=tk.W, pady=(0, 10))

        ttk.Label(info, text="Улицы / подходы", font=("Segoe UI", -20, "bold")).pack(anchor=tk.W, pady=(8, 4))

        cols = ("street", "angle", "dir", "lanes", "speed", "type")
        self.tree = ttk.Treeview(info, columns=cols, show="headings", height=18)
        for c, t, w in [
            ("street", "Улица", 220), ("angle", "Угол", 85), ("dir", "Напр", 50),
            ("lanes", "Полос", 80), ("speed", "км/ч", 55), ("type", "Тип", 70),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        legend = ttk.Frame(info)
        legend.pack(fill=tk.X, pady=(8, 0))
        tk.Label(legend, text=" ●", fg=COLOR_IN, font=("Arial", 14)).pack(side=tk.LEFT)
        ttk.Label(legend, text="въезд").pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(legend, text=" ●", fg=COLOR_OUT, font=("Arial", 14)).pack(side=tk.LEFT)
        ttk.Label(legend, text="выезд").pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(legend, text=" ●", fg=COLOR_SEL, font=("Arial", 14)).pack(side=tk.LEFT)
        ttk.Label(legend, text="выбрано").pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(self.root, text="Лог", padding=4)
        log_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 5))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=4, font=("Consolas", -16))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

    # ---------- утилиты ----------

    def log(self, msg):
        # потокобезопасно: только кладём в очередь
        self.log_queue.put(msg)

    def _drain_log_forever(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_forever)

    def _set_status(self, text, color="gray"):
        self.status.configure(text=text, foreground=color)

    def _set_buttons(self, enabled, cancel=False):
        s = tk.NORMAL if enabled else tk.DISABLED
        for b in (self.btn_choose, self.btn_rebuild, self.btn_sumo, self.btn_editor):
            b.configure(state=s)
        self.btn_cancel.configure(state=tk.NORMAL if cancel else tk.DISABLED)

    def _run_async(self, fn, *args):
        def worker():
            self.root.after(0, lambda: self._set_buttons(False, cancel=True))
            try:
                fn(*args)
            except Exception as e:
                self.log(f"❌ {e}")
                self.root.after(0, lambda: self._set_status("Ошибка", "red"))
            finally:
                self.root.after(0, lambda: self._set_buttons(True, cancel=False))
                self._current_proc = None
        threading.Thread(target=worker, daemon=True).start()

    def _run_script(self, script_path, label, *script_args):
        """Запускает скрипт, стримит stdout в лог, ждёт завершения."""
        self.log(f"▶ {label}...")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self._current_proc = subprocess.Popen(
            [sys.executable, "-E", "-X", "utf8", str(script_path), *script_args],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env, bufsize=1,
        )
        for line in self._current_proc.stdout:
            line = line.rstrip("\n")
            if line:
                self.log(line)
        self._current_proc.wait()
        if self._current_proc.returncode != 0:
            raise RuntimeError(f"{label} завершился с кодом {self._current_proc.returncode}")
        # ИСПРАВЛЕНО: был лишний "\" перед {label}
        self.log(f"✓ {label} — готово")

    # ---------- действия ----------

    def _on_map_pick(self, coords):
        lat, lon = coords
        with open(COORDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"lat": lat, "lon": lon}, f, indent=2)
        self.log(f"Координаты выбраны на карте: {lat:.6f}, {lon:.6f}")
        if messagebox.askyesno("Перекрёсток выбран", "Сразу пересобрать сеть и открыть SUMO?"):
            self._run_async(self._rebuild_task, True)
        else:
            self.refresh_from_disk()

    def on_choose_browser(self):
        self._run_async(self._choose_browser_task)

    def _choose_browser_task(self):
        self.root.after(0, lambda: self._set_status("Ожидание клика в браузере...", "blue"))
        self.log("Открываю карту в браузере — кликните на перекрёсток")
        self._run_script(PROJECT_ROOT / "osm" / "intersection_finder.py", "Выбор перекрёстка", "--city", "Москва")
        if not COORDS_FILE.exists():
            raise RuntimeError("Координаты не сохранились")
        self.root.after(0, self._ask_rebuild_after_pick)

    def _ask_rebuild_after_pick(self):
        if messagebox.askyesno("Перекрёсток выбран", "Сразу пересобрать сеть и открыть SUMO?"):
            self._run_async(self._rebuild_task, True)
        else:
            self.refresh_from_disk()

    def on_rebuild(self):
        self._run_async(self._rebuild_task, False)

    def _rebuild_task(self, open_sumo_after):
        self.root.after(0, lambda: self._set_status("Пересборка...", "blue"))
        self._run_script(PROJECT_ROOT / "osm" / "geometry_builder.py", "Геометрия OSM")
        self._run_script(PROJECT_ROOT / "sumo" / "net_builder.py", "SUMO-сеть")
        self._run_script(PROJECT_ROOT / "sumo" / "route_builder.py", "Маршруты и детекторы")
        self.root.after(0, self.refresh_from_disk)
        self.root.after(0, lambda: self._set_status("Готово ✓", "green"))
        if open_sumo_after:
            self.root.after(400, self._open_sumo_impl)

    def on_open_sumo(self):
        if not model_file(PROJECT_ROOT, "config").exists():
            messagebox.showwarning("Нет конфига", f"{CONFIG_FILE} не найден.\nСначала пересоберите сеть.")
            return
        self._open_sumo_impl()

    def _open_sumo_impl(self):
        try:
            subprocess.Popen(["sumo-gui", "-c", str(model_file(PROJECT_ROOT, "config"))], cwd=str(PROJECT_ROOT))
            self.log("SUMO GUI запущен")
        except FileNotFoundError:
            messagebox.showerror("SUMO не найден", "Установите SUMO и добавьте в PATH")

    def on_open_editor(self):
        subprocess.Popen([sys.executable, "-E", "-X", "utf8", str(PROJECT_ROOT / "ui" / "main.py")],
                         cwd=str(PROJECT_ROOT))
        self.log("✏ Открыт редактор полос")

    def on_cancel(self):
        if self._current_proc:
            self._current_proc.terminate()
            self.log("⚠ Прервано пользователем")

    # ---------- карта ----------

    def _clear_map(self):
        if not self.map_widget:
            return
        for _, line in self.approach_lines:
            try:
                self.map_widget.delete(line)
            except Exception:
                pass
        self.approach_lines = []
        if self._highlight_line:
            try:
                self.map_widget.delete(self._highlight_line)
            except Exception:
                pass
            self._highlight_line = None

    def refresh_from_disk(self):
        geometry_file = model_file(PROJECT_ROOT, "geometry")
        if not geometry_file.exists():
            self.info_label.configure(text="geometry.json не найден.\n\n"
                                            "ПКМ на карте → «Сделать перекрёстком»\n"
                                            "или кнопка «Выбрать в браузере».")
            self._clear_map()
            return

        with open(geometry_file, "r", encoding="utf-8") as f:
            approaches = json.load(f)
        if not approaches:
            self.info_label.configure(text="geometry.json пуст")
            return

        center_lat = approaches[0].get("center_lat")
        center_lon = approaches[0].get("center_lon")
        if center_lat is None and COORDS_FILE.exists():
            with open(COORDS_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            center_lat, center_lon = c.get("lat"), c.get("lon")

        has_lights = any(a.get("traffic_lights") for a in approaches)
        type_str = "регулируемый" if has_lights else "нерегулируемый (priority)"

        self.info_label.configure(
            text=(f"Центр: {center_lat:.6f}, {center_lon:.6f}\n" if center_lat else "")
                 + f"Тип: {type_str}\n"
                 + f"Подходов: {len(approaches)}"
        )

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.approaches_by_iid = {}

        for a in approaches:
            iid = self.tree.insert("", tk.END, values=(
                a["street_name"][:22],
                f"{a['angle_deg']:.0f}°",
                a["direction"][:3],
                a["lanes"],
                a.get("maxspeed", "—"),
                "одностор." if a.get("oneway") else "двустор.",
            ))
            self.approaches_by_iid[iid] = a

        if self.map_widget and center_lat is not None:
            self._draw_map(approaches, center_lat, center_lon)

    def _draw_map(self, approaches, center_lat, center_lon):
        self._clear_map()
        self.map_widget.set_position(center_lat, center_lon)
        self.map_widget.set_zoom(17)

        for a in approaches:
            shape = a.get("shape_latlon") or []
            if len(shape) < 2:
                continue
            color = COLOR_IN if a["direction"] == "ВЪЕЗД" else COLOR_OUT
            path = [tuple(p) for p in shape]
            line = self.map_widget.set_path(path, color=color, width=3)
            self.approach_lines.append((a, line))

        self.map_widget.set_marker(center_lat, center_lon, text="Центр")

    def _on_tree_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        a = self.approaches_by_iid.get(sel[0])
        if not a or not self.map_widget:
            return
        shape = a.get("shape_latlon") or []
        if len(shape) < 2:
            return

        if self._highlight_line:
            try:
                self.map_widget.delete(self._highlight_line)
            except Exception:
                pass

        path = [tuple(p) for p in shape]
        self._highlight_line = self.map_widget.set_path(path, color=COLOR_SEL, width=6)
        mid = path[len(path) // 2]
        self.map_widget.set_position(*mid)


def main():
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
