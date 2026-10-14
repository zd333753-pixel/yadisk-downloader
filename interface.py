"""Desktop shell for the browser downloader. No browser or queue logic here."""
import ctypes
import json
import queue
import shutil
import threading
import time
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox

def show(app):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\YandexDiskBrowserCopier')
    if ctypes.get_last_error()==183:
        ctypes.windll.user32.MessageBoxW(None,'Программа уже открыта. Используйте её существующее окно.','YaDisk Downloader',0)
        return
    config_path=app.BASE/'settings.json'
    try:config=json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):config={}
    root=tk.Tk();root.title('YaDisk Downloader');root.geometry('960x740');root.minsize(900,710)
    icon_path=Path(getattr(app.sys,'_MEIPASS',app.BASE))/'app.ico'
    if icon_path.is_file():root.iconbitmap(str(icon_path))
    bg,white,ink,muted,blue,line='#f7f9fc','#ffffff','#162132','#687586','#0069ed','#dce3ed'
    root.configure(bg=bg)
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('TFrame',background=bg)
    style.configure('Card.TFrame',background=white)
    style.configure('TLabel',background=bg,foreground=ink,font=('Segoe UI',11))
    style.configure('Sub.TLabel',foreground=muted,font=('Segoe UI',10))
    style.configure('Title.TLabel',font=('Segoe UI',26,'bold'))
    style.configure('TButton',font=('Segoe UI',10),padding=(12,7),background=white,foreground=ink,bordercolor=line,relief='flat')
    style.map('TButton',background=[('active','#edf3fc')])
    style.configure('Primary.TButton',font=('Segoe UI',12,'bold'),padding=(24,14),background=blue,foreground=white,bordercolor=blue)
    style.map('Primary.TButton',background=[('disabled','#9dbbea'),('active','#0058ce')],foreground=[('disabled',white)])
    style.configure('TCheckbutton',background=bg,foreground=ink,font=('Segoe UI',11))
    unchecked=tk.PhotoImage(width=20,height=20);checked=tk.PhotoImage(width=20,height=20)
    unchecked.put(line,to=(1,1,19,19));unchecked.put(white,to=(2,2,18,18));checked.put(blue,to=(1,1,19,19))
    for x,y in [(5,10),(6,11),(7,12),(8,13),(9,12),(10,11),(11,10),(12,9),(13,8),(14,7)]:checked.put(white,to=(x,y,x+2,y+2))
    root._checkbox_images=(unchecked,checked)
    style.element_create('YaCheck.indicator','image',unchecked,('selected',checked))
    style.layout('TCheckbutton',[('Checkbutton.padding',{'sticky':'nswe','children':[
        ('YaCheck.indicator',{'side':'left','sticky':''}),('Checkbutton.label',{'side':'left','sticky':'nswe'})]})])
    style.configure('TRadiobutton',background=bg,foreground=ink,font=('Segoe UI',10))
    style.configure('TCombobox',padding=6,fieldbackground=white,font=('Segoe UI',11))
    style.configure('Download.Horizontal.TProgressbar',background='#21b547',troughcolor='#e2e7ed',bordercolor='#e2e7ed',lightcolor='#21b547',darkcolor='#21b547',thickness=14)
    source=tk.StringVar(value=config.get('source',''))
    source_mode=tk.StringVar(value=config.get('source_mode','private' if 'source' in config else 'public'))
    public_link=tk.StringVar(value=config.get('public_link',''))
    target=tk.StringVar(value=config.get('target',str(app.BASE/'Загрузки YaDisk')))
    downloads=tk.StringVar(value=config.get('downloads',str(app.default_downloads())))
    parallel=tk.StringVar(value=str(config.get('parallel',1)))
    create_subfolder=tk.BooleanVar(value=config.get('create_subfolder',source_mode.get()=='public'))
    headless=tk.BooleanVar(value=config.get('headless',True))
    skip_errors=tk.BooleanVar(value=config.get('skip_errors',True))
    remember_errors=tk.BooleanVar(value=config.get('remember_errors',True))
    status=tk.StringVar(value='Вставьте ссылку или выберите папку своего Диска.')
    file_title=tk.StringVar(value='Готов к скачиванию')
    stats=tk.StringVar(value='Файлы: 0 / 0   •   Папки: 0')
    percent=tk.StringVar(value='0%')
    failures=tk.StringVar(value='Не скачано: 0')
    free=tk.StringVar()
    current_target=[Path(config.get('last_copy_path',target.get()))]
    mode_preferences=config.get('mode_preferences',{})
    mode_preferences.setdefault('private',{'target':config.get('target',target.get()),'create_subfolder':False})
    public_base=str(Path(config['target']).parent) if config.get('source') and config.get('target') else str(app.BASE/'Загрузки YaDisk')
    mode_preferences.setdefault('public',{'target':public_base,'create_subfolder':True})
    previous_mode=[source_mode.get()]
    events=queue.Queue();stop=threading.Event();worker=[None];controls=[]
    def emit(value,kind='log'):events.put((kind,value))
    def browse(var):
        selected=filedialog.askdirectory(parent=root,initialdir=var.get() if Path(var.get()).is_dir() else str(app.BASE))
        if selected:var.set(selected)
    def save_settings():
        values={'source':source.get().strip().strip('/'),'source_mode':source_mode.get(),'public_link':public_link.get().strip(),
                'target':target.get().strip(),'downloads':downloads.get().strip(),'parallel':int(parallel.get()),
                'create_subfolder':create_subfolder.get(),'headless':headless.get(),'skip_errors':skip_errors.get(),
                'remember_errors':remember_errors.get(),'last_copy_path':str(current_target[0]),'mode_preferences':mode_preferences}
        mode_preferences[source_mode.get()]={'target':target.get(),'create_subfolder':create_subfolder.get()}
        config_path.write_text(json.dumps(values,ensure_ascii=False,indent=2),encoding='utf-8');return values
    def ask_error(remote,reason,allow_retry):
        response=queue.Queue(maxsize=1);emit((remote,reason,allow_retry,response),'ask_error')
        while True:
            if stop.is_set() or ctypes.windll.user32.GetAsyncKeyState(0x77)&0x8000:
                stop.set();raise app.Halt('Остановлено. Прогресс сохранён.')
            try:return response.get(timeout=.2)
            except queue.Empty:pass
    def error_dialog(value):
        remote,reason,allow_retry,response=value
        dialog=tk.Toplevel(root);dialog.title('Не удалось скачать файл');dialog.transient(root);dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=20);body.pack(fill='both',expand=True)
        ttk.Label(body,text=remote,font=('Segoe UI',11,'bold'),wraplength=600).pack(anchor='w')
        ttk.Label(body,text=reason,wraplength=600).pack(anchor='w',pady=12)
        apply_all=tk.BooleanVar(value=remember_errors.get())
        ttk.Checkbutton(body,text='Применять ответ ко всем следующим ошибкам',variable=apply_all).pack(anchor='w')
        ttk.Label(body,text='Повторить можно до трёх раз. Очередь для докачивания сохраняется.',wraplength=600).pack(anchor='w',pady=10)
        choices=ttk.Frame(body);choices.pack(fill='x')
        def choose(action):
            remember_errors.set(apply_all.get());save_settings();response.put((action,apply_all.get()));dialog.destroy()
        for title,action in [('Пропустить','skip'),('Повторить','retry'),('Остановить','stop')]:
            b=ttk.Button(choices,text=title,command=lambda a=action:choose(a));b.pack(side='left',padx=(0,8))
            if action=='retry' and not allow_retry:b.configure(state='disabled')
        dialog.protocol('WM_DELETE_WINDOW',lambda:choose('stop'));dialog.grab_set();dialog.focus_set()
    def open_failures():
        path=current_target[0]/'Не скачано.csv'
        if path.is_file():os.startfile(path)
        else:messagebox.showinfo('Список ошибок','Список появится после сканирования или ошибки скачивания.',parent=root)
    def launch(mode='download'):
        if worker[0] and worker[0].is_alive():return
        values=save_settings();link=values['public_link'] if values['source_mode']=='public' else ''
        try:
            if values['source_mode']=='public' and not link:raise app.Halt('Вставьте публичную ссылку на файл или папку.')
            link=app.public_url(link)
        except app.Halt as e:messagebox.showerror('Публичная ссылка',str(e),parent=root);return
        if not values['target'] or not values['downloads']:
            messagebox.showerror('Папки','Выберите папку для сохранения и временную папку в настройках.',parent=root);return
        stop.clear()
        for b in controls:b.configure(state='disabled')
        pause.configure(state='normal',text='Готово' if mode=='login' else 'Ⅱ');cancel.configure(state='normal')
        status.set('Запуск отдельного браузера…');file_title.set('Подготовка скачивания')
        def task():
            try:
                app.Runner(values['source'],values['target'],values['downloads'],stop,emit,
                           app.ErrorPolicy(values['skip_errors'],ask_error),headless=values['headless'],parallel=values['parallel'],
                           public_link=link,create_subfolder=values['create_subfolder']).run(mode)
            except app.Halt as e:emit(str(e),'status');emit(str(e))
            except Exception as e:
                emit('Ошибка: '+str(e),'status');emit('Ошибка: '+repr(e))
                import traceback
                with (app.BASE/'error.log').open('a',encoding='utf-8') as out:traceback.print_exc(file=out)
            finally:emit(None,'idle')
        worker[0]=threading.Thread(target=task,daemon=True);worker[0].start()
    def halt():stop.set();status.set('Остановка после начатой группы. Прогресс будет сохранён.')
    def settings_dialog():
        dialog=tk.Toplevel(root);dialog.title('Настройки скачивания');dialog.transient(root);dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=20);body.pack(fill='both',expand=True)
        ttk.Checkbutton(body,text='Работать в фоне (без окна браузера)',variable=headless).pack(anchor='w')
        ttk.Checkbutton(body,text='Пропускать ошибки файлов и продолжать очередь',variable=skip_errors).pack(anchor='w',pady=8)
        ttk.Checkbutton(body,text='Применять выбранный ответ ко всем следующим ошибкам',variable=remember_errors).pack(anchor='w')
        ttk.Label(body,text='Временная папка браузера программы').pack(anchor='w',pady=(16,4))
        row=ttk.Frame(body);row.pack(fill='x')
        ttk.Entry(row,textvariable=downloads,width=50).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='Выбрать',command=lambda:browse(downloads)).pack(side='left',padx=8)
        ttk.Label(body,text='При выключенном автопропуске появится выбор: повторить, пропустить или остановить.\nВаш обычный Chrome не используется.',wraplength=540,style='Sub.TLabel').pack(anchor='w',pady=14)
        ttk.Button(body,text='Готово',command=lambda:(save_settings(),dialog.destroy())).pack(anchor='e')
        dialog.grab_set()
    frame=ttk.Frame(root,padding=(28,22));frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Скачать с Яндекс Диска',style='Title.TLabel').pack(anchor='w',pady=(0,12))
    modes=ttk.Frame(frame);modes.pack(fill='x',pady=(0,12))
    def set_mode():
        if previous_mode[0]!=source_mode.get():
            mode_preferences[previous_mode[0]]={'target':target.get(),'create_subfolder':create_subfolder.get()}
            saved=mode_preferences[source_mode.get()]
            target.set(saved['target']);create_subfolder.set(saved['create_subfolder']);current_target[0]=Path(target.get())
            previous_mode[0]=source_mode.get()
        if source_mode.get()=='public':
            address.configure(textvariable=public_link);private_hint.configure(text='Публичная ссылка на файл или папку');download_button.configure(text='Скачать / продолжить')
        else:
            address.configure(textvariable=source);private_hint.configure(text='Папка моего Диска — пусто означает весь Диск');download_button.configure(text='Скачать / продолжить')
    for text,value in [('Публичная ссылка','public'),('Мой Яндекс Диск','private')]:
        radio=ttk.Radiobutton(modes,text=text,variable=source_mode,value=value,command=set_mode);radio.pack(side='left',padx=(0,20));controls.append(radio)
    input_row=ttk.Frame(frame);input_row.pack(fill='x')
    link_border=tk.Frame(input_row,bg=white,highlightbackground=line,highlightthickness=1);link_border.pack(side='left',fill='x',expand=True,ipady=10)
    tk.Label(link_border,text='↗',font=('Segoe UI',21),bg=white,fg=muted).pack(side='left',padx=(13,8))
    address=tk.Entry(link_border,textvariable=public_link,font=('Segoe UI',13),bg=white,fg=ink,relief='flat',highlightthickness=0)
    address.pack(side='left',fill='x',expand=True,padx=(0,14));controls.append(address)
    download_button=ttk.Button(input_row,text='Скачать / продолжить',style='Primary.TButton',command=launch)
    download_button.pack(side='right',padx=(14,0));controls.append(download_button)
    private_hint=ttk.Label(frame,text='Публичная ссылка на файл или папку',style='Sub.TLabel');private_hint.pack(anchor='w',pady=(5,14))
    row=ttk.Frame(frame);row.pack(fill='x',pady=(0,14))
    ttk.Label(row,text='Папка для сохранения:').pack(side='left',padx=(0,18))
    border=tk.Frame(row,bg=white,highlightbackground=line,highlightthickness=1);border.pack(side='left',fill='x',expand=True,ipady=8)
    target_entry=tk.Entry(border,textvariable=target,font=('Segoe UI',12),bg=white,fg=ink,relief='flat');target_entry.pack(side='left',fill='x',expand=True,padx=12);controls.append(target_entry)
    folder_button=ttk.Button(border,text='Выбрать…',command=lambda:browse(target));folder_button.pack(side='right',padx=4);controls.append(folder_button)
    row=ttk.Frame(frame);row.pack(fill='x',pady=(0,12))
    subfolder=ttk.Checkbutton(row,text='Создать подпапку с названием файла/папки',variable=create_subfolder);subfolder.pack(side='left');controls.append(subfolder)
    ttk.Label(row,text='Одновременно:').pack(side='left',padx=(24,8))
    concurrency=ttk.Combobox(row,textvariable=parallel,values=app.PARALLEL_COUNTS,state='readonly',width=4);concurrency.pack(side='left');controls.append(concurrency)
    tools=ttk.Frame(frame);tools.pack(fill='x',pady=(0,18))
    for title,command in [('Открыть браузер / вход',lambda:launch('login')),('Сканировать',lambda:launch('scan')),('Пересканировать',lambda:launch('rescan')),('Настройки',settings_dialog)]:
        b=ttk.Button(tools,text=title,command=command);b.pack(side='left',padx=(0,8));controls.append(b)
    # A white progress card mirrors the supplied desktop reference. Percentage
    # represents the verified file queue, never fabricated per-byte progress.
    card=tk.Frame(frame,bg=white,highlightbackground=line,highlightthickness=1);card.pack(fill='x',pady=(0,15))
    icon=tk.Canvas(card,width=65,height=84,bg=white,highlightthickness=0);icon.grid(row=0,column=0,rowspan=4,padx=(20,14),pady=22)
    icon.create_polygon(8,4,40,4,58,22,58,75,8,75,fill='#329cff',outline='#1987e9')
    icon.create_polygon(40,4,40,22,58,22,fill='#9bd0ff',outline='')
    icon.create_line(32,33,32,54,fill=white,width=4);icon.create_line(24,47,32,55,40,47,fill=white,width=4)
    icon.create_line(21,63,44,63,fill=white,width=4)
    card.columnconfigure(1,weight=1)
    tk.Label(card,textvariable=file_title,font=('Segoe UI',14,'bold'),bg=white,fg=ink,anchor='w').grid(row=0,column=1,sticky='ew',pady=(18,5))
    bar=tk.Frame(card,bg=white);bar.grid(row=1,column=1,sticky='ew',padx=(0,12));bar.columnconfigure(0,weight=1)
    progress=ttk.Progressbar(bar,maximum=1,style='Download.Horizontal.TProgressbar');progress.grid(row=0,column=0,sticky='ew')
    tk.Label(bar,textvariable=percent,font=('Segoe UI',12),bg=white,fg=muted,width=5).grid(row=0,column=1,padx=(10,0))
    tk.Label(card,textvariable=status,font=('Segoe UI',10),bg=white,fg=muted,anchor='w',justify='left',wraplength=650).grid(row=2,column=1,sticky='ew',pady=(7,3))
    tk.Label(card,textvariable=stats,font=('Segoe UI',10),bg=white,fg=muted,anchor='w').grid(row=3,column=1,sticky='ew',pady=(0,18))
    actions=tk.Frame(card,bg=white);actions.grid(row=0,column=2,rowspan=4,padx=(5,18))
    pause=ttk.Button(actions,text='Ⅱ',width=3,command=halt,state='disabled');pause.pack(side='left',padx=(0,7))
    cancel=ttk.Button(actions,text='×',width=3,command=halt,state='disabled');cancel.pack(side='left')
    footer=ttk.Frame(frame);footer.pack(fill='x',pady=(0,12))
    ttk.Label(footer,textvariable=free,style='Sub.TLabel').pack(side='left')
    ttk.Label(footer,textvariable=failures,style='Sub.TLabel').pack(side='right',padx=(10,0))
    ttk.Button(footer,text='Список ошибок',command=open_failures).pack(side='right')
    ttk.Label(frame,text='Журнал:',font=('Segoe UI',11,'bold')).pack(anchor='w',pady=(0,5))
    journal=tk.Frame(frame,bg=white,highlightbackground=line,highlightthickness=1);journal.pack(fill='both',expand=True)
    box=tk.Text(journal,font=('Consolas',10),bg=white,fg=ink,relief='flat',wrap='word',state='disabled',padx=12,pady=10,height=7)
    scroll=ttk.Scrollbar(journal,command=box.yview);scroll.pack(side='right',fill='y');box.configure(yscrollcommand=scroll.set);box.pack(fill='both',expand=True)
    set_mode()
    def poll():
        while True:
            try:kind,value=events.get_nowait()
            except queue.Empty:break
            if kind=='source':source.set(value)
            elif kind=='target_path':current_target[0]=Path(value)
            elif kind=='ask_error':error_dialog(value)
            elif kind=='failures':failures.set(f'Не скачано: {value}')
            elif kind=='status':
                status.set(value)
                if str(value).startswith('Скачивание: '):file_title.set(str(value).rsplit('/',1)[-1])
                elif str(value).startswith('Готово.'):file_title.set('Скачивание завершено')
            elif kind=='stats':
                total,done,folders,estimate=value
                stats.set(f'Файлы: {done} / {total}   •   Папки: {folders}   •   Объём ≈ {estimate/1024**3:.2f} ГБ')
                progress.configure(maximum=max(total,1),value=done);percent.set(f'{int(done/max(total,1)*100)}%')
            elif kind=='idle':
                for child in root.winfo_children():
                    if isinstance(child,tk.Toplevel):child.destroy()
                for b in controls:b.configure(state='readonly' if b is concurrency else 'normal')
                pause.configure(state='disabled',text='Ⅱ');cancel.configure(state='disabled')
                download_button.configure(text='Скачать / продолжить')
            else:
                entry=time.strftime('[%H:%M:%S]')+'  '+str(value)+'\n'
                box.configure(state='normal');box.insert('end',entry);box.see('end');box.configure(state='disabled')
                with (app.BASE/'downloads.log').open('a',encoding='utf-8') as out:out.write(entry)
        try:
            path=Path(target.get())
            while not path.exists() and path!=path.parent:path=path.parent
            free.set(f'Свободно: {shutil.disk_usage(path).free/1024**3:.1f} ГБ')
        except OSError:pass
        root.after(400,poll)
    def close():
        if worker[0] and worker[0].is_alive():halt()
        else:save_settings();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);poll();root.mainloop()
