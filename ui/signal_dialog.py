"""Parameters for the experimental RL demo; subprocess keeps the UI responsive."""
import webbrowser
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path


def open_signal_dialog(parent, project_root):
    root = Path(project_root)
    window = tk.Toplevel(parent)
    window.title('RL: диапазоны длительности фаз')
    ttk.Label(window, text='Учебный автоперекрёсток: Q-learning в SUMO.\nКрасный здесь — отдельная фаза «всем красный».\nКрасный направления складывается из остальных фаз.', padding=12).pack()
    values = {}
    for key, label, default in [('green-min','Зелёный минимум, с',15),('green-max','Зелёный максимум, с',45),
                                 ('yellow-min','Жёлтый минимум, с',3),('yellow-max','Жёлтый максимум, с',5),
                                 ('red-min','Всем красный минимум, с',2),('red-max','Всем красный максимум, с',4),
                                 ('episodes','Эпизодов обучения',30)]:
        row=ttk.Frame(window,padding=3); row.pack(fill='x')
        ttk.Label(row,text=label,width=30).pack(side='left')
        values[key]=tk.StringVar(value=str(default))
        ttk.Entry(row,textvariable=values[key],width=10).pack(side='right')
    def launch():
        try:
            parsed={k:int(v.get()) for k,v in values.items()}
            for color in ['green','yellow','red']:
                if not 0 < parsed[color+'-min'] <= parsed[color+'-max']: raise ValueError('Проверьте минимум и максимум')
            if parsed['yellow-min']<3 or parsed['episodes']<1: raise ValueError('Жёлтый минимум 3 с; эпизодов не меньше 1')
        except ValueError as exc:
            messagebox.showerror('Параметры',str(exc),parent=window); return
        if getattr(window, '_training_process', None) and window._training_process.poll() is None:
            messagebox.showinfo('Обучение', 'Дождитесь завершения текущего запуска.', parent=window)
            return
        script=root/'sumo'/'rl_signals.py'
        log_dir=root/'runs'/'rl'; log_dir.mkdir(parents=True,exist_ok=True)
        config=root/'examples'/'signal_demo'/'demo.sumocfg'
        if not config.exists():
            messagebox.showinfo('Сначала создайте пример','Из корня проекта выполните:\npython examples/create_signal_demo.py',parent=window); return
        command=[sys.executable,'-u',str(script),'--config',str(config)]
        for k,v in parsed.items(): command.extend(['--'+k,str(v)])
        log=log_dir/'training.log'
        with log.open('w',encoding='utf-8') as output:
            window._training_process = subprocess.Popen(command,cwd=root,stdout=output,stderr=subprocess.STDOUT)
        def check():
            process = window._training_process
            if process.poll() is None:
                if window.winfo_exists(): window.after(1000, check)
                return
            if process.returncode == 0:
                webbrowser.open((log_dir/'report.html').resolve().as_uri())
            elif window.winfo_exists():
                messagebox.showerror('RL', f'Запуск завершился с ошибкой. Подробности: {log}', parent=window)
        window.after(1000, check)
        messagebox.showinfo('Обучение запущено',f'Лог: {log}\nИтог: runs/rl/results.json\nПрогресс и результат появятся в этих файлах.',parent=window)
    ttk.Button(window,text='Запустить RL на учебном примере',command=launch).pack(pady=12)
