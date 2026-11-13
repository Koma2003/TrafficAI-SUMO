"""Controls for Q-learning on the separate training intersection."""
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from datetime import datetime
from uuid import uuid4
from ui.report_viewer import show_report


def training_command(project_root, parameters, output, backend):
    root=Path(project_root).resolve()
    local=root/'.venv'/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    executable=local if local.exists() else Path(sys.executable)
    command=[str(executable),'-E','-X','utf8','-u',str(root/'sumo/rl_signals.py'),
             '--config',str(root/'examples/signal_demo/demo.sumocfg'),'--output',str(output),'--backend',backend]
    for key,value in parameters.items():command.extend(['--'+key,str(value)])
    return command


def open_signal_dialog(parent, project_root):
    from ui.appearance import configure_ui
    configure_ui(parent)
    existing=getattr(parent,'_rl_training_window',None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify();existing.lift();return existing
    root=Path(project_root).resolve()
    window=tk.Toplevel(parent);parent._rl_training_window=window
    window.protocol('WM_DELETE_WINDOW',window.withdraw)
    window.title('RL-обучение')
    ttk.Label(window,text='Q-learning на учебном перекрёстке SUMO без пешеходов.\n'
              'Обучение выполняется отдельно от московской модели.\n'
              'Красный ниже — фаза «всем красный», между направлениями.',padding=12).pack()
    values={}
    entries=[]
    for key,label,default in [('green-min','Зелёный минимум, с',15),('green-max','Зелёный максимум, с',45),
        ('yellow-min','Жёлтый минимум, с',3),('yellow-max','Жёлтый максимум, с',5),
        ('red-min','Всем красный минимум, с',2),('red-max','Всем красный максимум, с',4),
        ('episodes','Эпизодов обучения',30)]:
        row=ttk.Frame(window,padding=3);row.pack(fill='x')
        ttk.Label(row,text=label,width=30).pack(side='left')
        values[key]=tk.StringVar(value=str(default))
        entry=ttk.Entry(row,textvariable=values[key],width=10);entry.pack(side='right');entries.append(entry)
    status=tk.StringVar(value='Задайте диапазоны и запустите обучение.')
    ttk.Label(window,textvariable=status,padding=10,wraplength=640).pack(fill='x')
    progress=ttk.Progressbar(window,mode='indeterminate');progress.pack(fill='x',padx=12,pady=5)
    log_view=tk.Text(window,height=6,width=75,wrap='word',state='disabled',font=('Consolas',-12))
    log_view.pack(fill='both',expand=True,padx=12,pady=6)
    process=[None];report=[None];log=[None];cancelled=[False]
    def open_report():
        if report[0] and report[0].exists():show_report(window,report[0],'Результаты RL-обучения')
    def set_busy(busy):
        start.configure(state='disabled' if busy else 'normal')
        stop.configure(state='normal' if busy else 'disabled')
        for entry in entries:entry.configure(state='disabled' if busy else 'normal')
        if busy:progress.start(15)
        else:progress.stop()
    def check():
        if not window.winfo_exists():return
        content=log[0].read_text(encoding='utf-8',errors='replace') if log[0] and log[0].exists() else ''
        log_view.configure(state='normal');log_view.delete('1.0','end');log_view.insert('end','\n'.join(content.splitlines()[-12:]));log_view.see('end');log_view.configure(state='disabled')
        if process[0].poll() is None:
            window.after(500,check);return
        set_busy(False)
        if cancelled[0]:status.set('Обучение остановлено.');return
        if process[0].returncode==0 and report[0].exists():
            status.set('Обучение завершено. Результаты и политика сохранены.')
            reopen.configure(state='normal');open_report()
        else:
            status.set('Не удалось завершить обучение. Причина показана в журнале ниже.')
            messagebox.showerror('RL-обучение','\n'.join(content.splitlines()[-8:]) or 'Нет журнала запуска',parent=window)
    def launch():
        if process[0] is not None and process[0].poll() is None:return
        try:
            parsed={key:int(var.get()) for key,var in values.items()}
            for color in ['green','yellow','red']:
                if not 0<parsed[color+'-min']<=parsed[color+'-max']:raise ValueError('Проверьте минимум и максимум')
            if parsed['episodes']<1:raise ValueError('Нужен минимум один эпизод')
            from sumo.rl_signals import duration_choices, load_program, valid_action_indices
            actions=[(g,y,r) for g in duration_choices(parsed['green-min'],parsed['green-max'],10)
                     for y in duration_choices(parsed['yellow-min'],parsed['yellow-max'],1)
                     for r in duration_choices(parsed['red-min'],parsed['red-max'],1)]
            if len(actions)>500:raise ValueError('Слишком широкий диапазон: уменьшите границы')
            if not (root/'examples/signal_demo/demo.sumocfg').exists():
                local=training_command(root,{},root/'runs','traci')[0]
                subprocess.run([local,'-E','-X','utf8',str(root/'examples/create_signal_demo.py')],cwd=root,check=True,capture_output=True)
            _,phases=load_program(root/'examples/signal_demo/demo.sumocfg')
            for phase in phases:valid_action_indices(actions,phase)
        except (ValueError,OSError,subprocess.SubprocessError) as exc:
            messagebox.showerror('RL-обучение',str(exc),parent=window);return
        output=root/'runs'/('rl_training_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]);output.mkdir(parents=True)
        log[0]=output/'training.log';report[0]=output/'report.html';cancelled[0]=False
        command=training_command(root,parsed,output,'traci')
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
        probe=subprocess.run([command[0],'-E','-X','utf8','-c','import libsumo'],capture_output=True,**options)
        if probe.returncode==0:command[command.index('--backend')+1]='libsumo'
        try:
            with log[0].open('w',encoding='utf-8') as stream:
                process[0]=subprocess.Popen(command,cwd=root,stdout=stream,stderr=subprocess.STDOUT,**options)
        except OSError as exc:
            status.set('Не удалось запустить процесс.');messagebox.showerror('RL-обучение',str(exc),parent=window);return
        set_busy(True);reopen.configure(state='disabled');status.set('Идёт обучение. Прогресс показан в журнале.')
        window.after(500,check)
    def cancel():
        if process[0] is not None and process[0].poll() is None:
            cancelled[0]=True;process[0].terminate();status.set('Останавливаю обучение…')
    buttons=ttk.Frame(window,padding=10);buttons.pack(fill='x')
    start=ttk.Button(buttons,text='Начать обучение',command=launch);start.pack(side='left')
    stop=ttk.Button(buttons,text='Остановить',state='disabled',command=cancel);stop.pack(side='left',padx=8)
    reopen=ttk.Button(buttons,text='Открыть результаты',state='disabled',command=open_report);reopen.pack(side='left')
    return window
