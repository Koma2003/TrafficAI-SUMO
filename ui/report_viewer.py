"""Open saved reports inside the application, independently of browser setup."""
import os
import tkinter as tk
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from tkinter import ttk, messagebox


class ReportText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
        self.skip=0

    def handle_starttag(self,tag,attrs):
        if tag in {'style','script','svg'}:self.skip+=1
        if not self.skip:
            if tag in {'h1','h2','h3','p','tr','li','br','footer'}:self.parts.append('\n')
            elif tag in {'td','th'}:self.parts.append(' | ')

    def handle_endtag(self,tag):
        if tag in {'style','script','svg'}:self.skip=max(0,self.skip-1)
        elif not self.skip and tag in {'h1','h2','h3','p','tr','li','footer'}:self.parts.append('\n')

    def handle_data(self,data):
        if not self.skip and data.strip():self.parts.append(data.strip()+' ')

    def text(self):
        return '\n\n'.join(line.strip(' |') for line in ''.join(self.parts).splitlines() if line.strip(' |'))


def show_report(parent,path,title='Отчёт'):
    path=Path(path).resolve()
    parser=ReportText();parser.feed(path.read_text(encoding='utf-8'))
    window=tk.Toplevel(parent);window.title(title);window.geometry('1000x760')
    container=ttk.Frame(window,padding=12);container.pack(fill='both',expand=True)
    ttk.Label(container,text=title,font=('Segoe UI',-20,'bold')).pack(anchor='w',pady=(0,10))
    body=ttk.Frame(container);body.pack(fill='both',expand=True)
    text=tk.Text(body,wrap='word',font=('Segoe UI',-15),padx=14,pady=12)
    scroll=ttk.Scrollbar(body,orient='vertical',command=text.yview)
    scroll.pack(side='right',fill='y');text.pack(side='left',fill='both',expand=True)
    text.configure(yscrollcommand=scroll.set);text.insert('1.0',parser.text());text.configure(state='disabled')
    ttk.Label(container,text=str(path),wraplength=940).pack(anchor='w',pady=8)
    def open_file(target):
        try:
            if os.name=='nt':os.startfile(str(target))
            elif not webbrowser.open(target.as_uri()):raise OSError('Не удалось открыть приложение')
        except OSError as exc:messagebox.showerror(title,str(exc),parent=window)
    buttons=ttk.Frame(container);buttons.pack(fill='x')
    ttk.Button(buttons,text='Открыть HTML в браузере',command=lambda:open_file(path)).pack(side='left')
    ttk.Button(buttons,text='Папка отчёта',command=lambda:open_file(path.parent)).pack(side='left',padx=8)
    return window
