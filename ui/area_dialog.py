"""Area selection in the existing desktop map."""
import json
import threading
import webbrowser
import uuid
import queue
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from osm.area_analysis import fetch_area, summarize, save_report, validate_bbox
from ui.report_viewer import show_report


def initial_bbox(map_widget, project_root):
    """Use the currently visible map, with a validated disk/default fallback."""
    center = None
    if map_widget is not None:
        try:
            lat, lon = map_widget.get_position()
            center = [float(lat), float(lon)]
            validate_bbox([center[0]-.005, center[1]-.005, center[0]+.005, center[1]+.005])
        except (AttributeError, ValueError, TypeError, tk.TclError):
            center = None
    if center is None:
        coords = Path(project_root) / 'clicked_coords.json'
        try:
            point = json.loads(coords.read_text(encoding='utf-8'))
            center = [float(point['lat']), float(point['lon'])]
            validate_bbox([center[0]-.005, center[1]-.005, center[0]+.005, center[1]+.005])
        except (OSError, ValueError, TypeError, KeyError):
            center = [55.751244, 37.618423]
    return [center[0]-.005, center[1]-.005, center[0]+.005, center[1]+.005]


def draw_bbox(map_widget, bbox, previous=None):
    """Show the validated rectangle and fit it into the current map view."""
    (south, west, north, east), _ = validate_bbox(bbox)
    if map_widget is None:
        return None
    if previous is not None:
        previous.delete()
    path = map_widget.set_path([(south,west),(south,east),(north,east),(north,west),(south,west)],
                               color='#f97316', width=4)
    map_widget.fit_bounding_box((north,west),(south,east))
    return path


def open_area_dialog(parent, map_widget, project_root):
    from ui.appearance import configure_ui
    configure_ui(parent)
    existing = getattr(parent, '_area_report_window', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        return
    window = tk.Toplevel(parent)
    parent._area_report_window = window
    window.protocol('WM_DELETE_WINDOW', window.withdraw)
    window.title('Выделить район и получить отчёт')
    ttk.Label(window, text='Выделите область: задайте границы или выберите два угла ПКМ на карте.\nДля примера — до 25 км². Получение OSM может занять около минуты.', padding=12).pack()
    variables = [tk.StringVar(value=f'{v:.6f}') for v in initial_bbox(map_widget, project_root)]
    for name, var in zip(['Южная широта', 'Западная долгота', 'Северная широта', 'Восточная долгота'], variables):
        row = ttk.Frame(window, padding=8)
        row.pack(fill='x')
        ttk.Label(row, text=name, width=25).pack(side='left')
        ttk.Entry(row, textvariable=var).pack(side='left', fill='x', expand=True)
    polygon = [None]
    corners = []
    messages=queue.Queue()
    saved_report=[None]
    def draw():
        try:
            (s, w, n, e), _ = validate_bbox([v.get() for v in variables])
            polygon[0] = draw_bbox(map_widget, [s,w,n,e], polygon[0])
            return True
        except ValueError as exc:
            messagebox.showerror('Границы области', str(exc), parent=window)
            return False
    def corner(position):
        if not window.winfo_exists(): return
        if len(corners) == 2: corners.clear()
        corners.append(position)
        if len(corners) == 2:
            a,b = corners
            for v, number in zip(variables, [min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])]):
                v.set(f'{number:.6f}')
            if draw():
                status.set('Область выделена. Нажмите «Сформировать отчёт».')
            else:
                status.set('Проверьте границы выделенной области.')
        else: status.set('Первый угол выбран. Выберите второй через ПКМ.')
    if map_widget:
        map_widget.add_right_click_menu_command(label='Угол области для отчёта', command=corner, pass_coords=True)
    status = tk.StringVar(value='Отчёт содержит категории, названия и ссылки на объекты OSM.')
    ttk.Label(window, textvariable=status, padding=12, wraplength=520).pack()
    def generate():
        try: bbox, _ = validate_bbox([v.get() for v in variables])
        except ValueError as exc:
            messagebox.showerror('Границы области', str(exc), parent=window)
            return
        if not draw():
            return
        button.configure(state='disabled')
        progress.start(15)
        status.set('Загружаю инфраструктуру из OpenStreetMap…')
        def worker():
            try:
                root = Path(project_root)
                data = fetch_area(bbox, root / 'runs' / 'cache')
                target = root / 'runs' / ('area_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8])
                report = save_report(summarize(data, bbox), target)
                messages.put(('done',report))
            except Exception as exc:
                messages.put(('error',str(exc)))
        threading.Thread(target=worker, daemon=True).start()
    def finished(report):
        if window.winfo_exists():
            progress.stop()
            saved_report[0]=report
            reopen.configure(state='normal')
            status.set('Отчёт готов. Данные сохранены, отчёт открыт в приложении.')
            button.configure(state='normal')
            show_report(window,report,'Отчёт по району')
    def failed(message):
        if window.winfo_exists():
            progress.stop()
            button.configure(state='normal')
            status.set('Не удалось получить данные. Повторите позже или запустите офлайн-пример.')
            messagebox.showerror('Анализ района', message, parent=window)
    ttk.Button(window, text='Показать границы', command=draw).pack(pady=5)
    button = ttk.Button(window, text='Сформировать отчёт', command=generate)
    button.pack(pady=10)
    progress=ttk.Progressbar(window,mode='indeterminate')
    progress.pack(fill='x',padx=16,pady=6)
    reopen=ttk.Button(window,text='Открыть готовый отчёт',state='disabled',
                     command=lambda:show_report(window,saved_report[0],'Отчёт по району'))
    reopen.pack(pady=(0,12))
    def poll():
        if not window.winfo_exists():return
        try:
            while True:
                kind,payload=messages.get_nowait()
                if kind=='done':finished(payload)
                else:failed(payload)
        except queue.Empty:pass
        window.after(100,poll)
    window.after(100,poll)
    draw()
