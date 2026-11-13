"""Readable desktop controls for live demonstrations."""
import tkinter.font as tkfont
from tkinter import ttk


def configure_ui(root):
    if getattr(root, '_trafficai_appearance', False):
        return
    root._trafficai_appearance = True
    for name in ['TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont','TkCaptionFont','TkTooltipFont']:
        tkfont.nametofont(name, root=root).configure(family='Segoe UI', size=-13)
    tkfont.nametofont('TkFixedFont', root=root).configure(size=-12)
    style=ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Segoe UI', -13))
    style.configure('TButton', font=('Segoe UI', -13), padding=(6,3))
    style.configure('TEntry', padding=(4,2))
    style.configure('Treeview', font=('Segoe UI', -13), rowheight=23)
    style.configure('Treeview.Heading', font=('Segoe UI', -13, 'bold'), padding=(4,3))
    style.configure('TLabelframe.Label', font=('Segoe UI', -13, 'bold'))
    style.configure('TScrollbar', width=16)


def size_window(window, width=1440, height=960):
    window.geometry(f'{min(width,window.winfo_screenwidth()-60)}x{min(height,window.winfo_screenheight()-90)}')
