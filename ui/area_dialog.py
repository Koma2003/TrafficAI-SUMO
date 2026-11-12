"""Area selection in the existing desktop map."""
import json
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from osm.area_analysis import fetch_area, summarize, save_report, validate_bbox


def open_area_dialog(parent, map_widget, project_root):
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
    center = [54.79177, 55.87771]
    coords = Path(project_root) / 'clicked_coords.json'
    if coords.exists():
        point = json.loads(coords.read_text(encoding='utf-8'))
        center = [point['lat'], point['lon']]
    variables = [tk.StringVar(value=f'{v:.6f}') for v in [center[0]-.005, center[1]-.005, center[0]+.005, center[1]+.005]]
    for name, var in zip(['Южная широта', 'Западная долгота', 'Северная широта', 'Восточная долгота'], variables):
        row = ttk.Frame(window, padding=4)
        row.pack(fill='x')
        ttk.Label(row, text=name, width=24).pack(side='left')
        ttk.Entry(row, textvariable=var).pack(side='left', fill='x', expand=True)
    polygon = [None]
    corners = []
    def draw():
        try:
            (s, w, n, e), _ = validate_bbox([v.get() for v in variables])
            if map_widget:
                if polygon[0]: polygon[0].delete()
                polygon[0] = map_widget.set_path([(s,w),(s,e),(n,e),(n,w),(s,w)], color='#f97316', width=4)
        except ValueError as exc:
            messagebox.showerror('Границы области', str(exc), parent=window)
    def corner(position):
        if not window.winfo_exists(): return
        if len(corners) == 2: corners.clear()
        corners.append(position)
        if len(corners) == 2:
            a,b = corners
            for v, number in zip(variables, [min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])]):
                v.set(f'{number:.6f}')
            draw()
            status.set('Область выделена. Нажмите «Сформировать отчёт».')
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
        button.configure(state='disabled')
        status.set('Загружаю инфраструктуру из OpenStreetMap…')
        def worker():
            try:
                root = Path(project_root)
                data = fetch_area(bbox, root / 'runs' / 'cache')
                target = root / 'runs' / ('area_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
                report = save_report(summarize(data, bbox), target)
                parent.after(0, lambda: finished(report))
            except Exception as exc:
                parent.after(0, lambda message=str(exc): failed(message))
        threading.Thread(target=worker, daemon=True).start()
    def finished(report):
        if window.winfo_exists():
            status.set(f'Готово: {report}')
            button.configure(state='normal')
        webbrowser.open(report.resolve().as_uri())
    def failed(message):
        if window.winfo_exists():
            button.configure(state='normal')
            status.set('Не удалось получить данные. Повторите позже или запустите офлайн-пример.')
            messagebox.showerror('Анализ района', message, parent=window)
    ttk.Button(window, text='Показать границы', command=draw).pack(pady=5)
    button = ttk.Button(window, text='Сформировать отчёт', command=generate)
    button.pack(pady=10)
    draw()
