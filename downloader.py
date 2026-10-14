"""Yandex Disk copier with its own persistent Chrome; no Yandex API."""
from __future__ import annotations
import ctypes
import copy
import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESERVE = 512 * 1024**2
PARALLEL_COUNTS = (1, 5, 10, 50)

def public_url(value):
    value=value.strip()
    if not value: return ''
    u=urlparse(value if '://' in value else 'https://'+value)
    if u.scheme not in ('http','https') or u.hostname not in ('disk.yandex.ru','disk.360.yandex.ru','yadi.sk') or u.username or u.password or u.port:
        raise Halt('Укажите публичную ссылку Яндекс Диска: https://disk.yandex.ru/d/… или /i/…')
    if not re.match(r'^/[di]/[^/]+',u.path):
        raise Halt('Это не публичная ссылка на файл или папку Яндекс Диска.')
    path=quote(unquote(u.path.rstrip('/')),safe='/')
    extra=parse_qs(u.query).get('path',[''])[0]
    if extra: path+='/'+quote(extra.strip('/'),safe='/')
    return 'https://disk.yandex.ru'+path

def source_identity(source,link=''):
    return '@public-'+hashlib.sha256(public_url(link).encode()).hexdigest()[:24] if link else source.strip('/')

class Halt(Exception):
    pass

class FileFailure(Exception):
    """Failure confined to one file; safe to advance an idle queue."""
    pass

class ErrorPolicy:
    def __init__(self, skip_errors=False, ask=None):
        self.skip_errors, self.ask, self.remembered = skip_errors, ask, None

    def decide(self, remote, reason, failures):
        if self.skip_errors:
            return 'skip'
        if self.remembered:
            return 'skip' if self.remembered=='retry' and failures>=3 else self.remembered
        if self.ask is None:
            return 'stop'
        action, remember = self.ask(remote, reason, failures<3)
        if action not in {'skip','retry','stop'} or (action=='retry' and failures>=3):
            action = 'stop'
        if remember:
            self.remembered = action
        return action

def safe_name(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .') or '_'
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *[f'COM{i}' for i in range(1,10)], *[f'LPT{i}' for i in range(1,10)]}:
        value = '_' + value
    if value != name or len(value) > 180:
        p = Path(value)
        value = p.stem[:140] + '~' + hashlib.sha256(name.encode()).hexdigest()[:10] + p.suffix[:20]
    return value

def parse_size(text):
    m = re.fullmatch(r'\s*([\d.,]+)\s*(Б|КБ|МБ|ГБ|ТБ|B|KB|MB|GB|TB)\s*', text, re.I)
    if not m:
        return None
    units = {'Б':0,'КБ':1,'МБ':2,'ГБ':3,'ТБ':4,'B':0,'KB':1,'MB':2,'GB':3,'TB':4}
    number = m[1].replace(',', '.')
    # Website displays rounded sizes. Accept either decimal or binary units.
    power = units[m[2].upper()]
    n = float(number)
    decimals = len(number.split('.')[1]) if '.' in number else 0
    delta = 0.51 * 10 ** -decimals
    return max(0, (n-delta)*1000**power), (n+delta)*1024**power

def url_for(remote):
    return 'https://disk.yandex.ru/client/disk' + ('/' + quote(remote.strip('/'), safe='/') if remote.strip('/') else '')

def remote_from_url(value):
    if '://' not in value:
        value = 'https://' + value
    u = urlparse(value)
    if u.hostname != 'disk.yandex.ru' or not (u.path == '/client/disk' or u.path.startswith('/client/disk/')):
        raise Halt('Откройте вкладку Файлы на disk.yandex.ru. Поддерживаются личные папки Диска.')
    return unquote(u.path[len('/client/disk'):]).strip('/')

@dataclass
class Entry:
    name: str
    directory: bool
    size: str
    stamp: str

class Catalog:
    def __init__(self, root, source):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / '.yd-browser-progress.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS names(remote TEXT PRIMARY KEY,local TEXT COLLATE NOCASE UNIQUE);
          CREATE TABLE IF NOT EXISTS folders(remote TEXT PRIMARY KEY,state TEXT DEFAULT 'pending');
          CREATE TABLE IF NOT EXISTS files(remote TEXT PRIMARY KEY,parent TEXT,name TEXT,size TEXT,stamp TEXT,
            state TEXT DEFAULT 'pending',bytes INTEGER,local TEXT);
          CREATE TABLE IF NOT EXISTS failures(remote TEXT PRIMARY KEY,local TEXT,reason TEXT,at TEXT,count INTEGER);
        ''')
        old = self.db.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        if old and old[0] != source:
            self.db.close()
            raise Halt('В этой папке уже хранится копия другого источника. Выберите другую папку назначения.')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('source',?)", (source,))
        self.db.execute('INSERT OR IGNORE INTO names VALUES(?,?)', (source, ''))
        self.db.execute('INSERT OR IGNORE INTO folders(remote) VALUES(?)', (source,))
        self.db.commit()

    def close(self):
        self.db.close()

    def path(self, remote):
        row = self.db.execute('SELECT local FROM names WHERE remote=?', (remote,)).fetchone()
        if row:
            path = self.root / row[0]
        else:
            parent, _, name = remote.rpartition('/')
            p = self.path(parent)
            name = safe_name(name)
            path = p / name
            rel = str(path.relative_to(self.root))
            collision = self.db.execute('SELECT remote FROM names WHERE local=? COLLATE NOCASE', (rel,)).fetchone()
            reserved = p == self.root and name.casefold() in {
                '.yd-browser-progress.sqlite3','.yd-browser-progress.sqlite3-wal','.yd-browser-progress.sqlite3-shm',
                'не скачано.csv','.yd-failures.csv.tmp'}
            if collision or reserved:
                t = Path(name)
                path = p / (t.stem + '~' + hashlib.sha256(remote.encode()).hexdigest()[:10] + t.suffix)
            self.db.execute('INSERT INTO names VALUES(?,?)', (remote, str(path.relative_to(self.root))))
            self.db.commit()
        if not path.resolve().is_relative_to(self.root):
            raise Halt('Путь выходит за папку назначения (возможно, символическая ссылка).')
        return path

    def record(self, parent, entries):
        for e in entries:
            remote = (parent + '/' + e.name).strip('/')
            path = self.path(remote)
            if e.directory:
                path.mkdir(parents=True, exist_ok=True)
                self.db.execute('INSERT OR IGNORE INTO folders(remote) VALUES(?)', (remote,))
            else:
                old = self.db.execute('SELECT size,stamp FROM files WHERE remote=?', (remote,)).fetchone()
                if old and old != (e.size, e.stamp):
                    self.db.execute("UPDATE files SET size=?,stamp=?,state='pending' WHERE remote=?", (e.size,e.stamp,remote))
                self.db.execute('INSERT OR IGNORE INTO files(remote,parent,name,size,stamp) VALUES(?,?,?,?,?)',
                                (remote,parent,e.name,e.size,e.stamp))
        self.db.execute("UPDATE folders SET state='scanned' WHERE remote=?", (parent,))
        self.db.commit()

    def done(self, remote, path):
        self.db.execute("UPDATE files SET state='done',bytes=?,local=? WHERE remote=?",
                        (path.stat().st_size,str(path.relative_to(self.root)),remote))
        self.db.execute('DELETE FROM failures WHERE remote=?',(remote,))
        self.db.commit()
        return self.export_failures()

    def failed(self, remote, reason):
        local = str(self.path(remote).relative_to(self.root))
        at = time.strftime('%Y-%m-%d %H:%M:%S')
        self.db.execute("UPDATE files SET state='failed' WHERE remote=?",(remote,))
        self.db.execute('''INSERT INTO failures VALUES(?,?,?,?,1)
            ON CONFLICT(remote) DO UPDATE SET reason=excluded.reason,at=excluded.at,count=failures.count+1''',
            (remote,local,str(reason),at))
        self.db.commit()
        return self.export_failures()

    def export_failures(self):
        path=self.root/'Не скачано.csv'
        temp=self.root/'.yd-failures.csv.tmp'
        try:
            with temp.open('w',encoding='utf-8-sig',newline='') as out:
                writer=csv.writer(out,delimiter=';')
                writer.writerow(['Путь на Диске','Локальная папка и имя','Причина','Время','Число ошибок'])
                for row in self.db.execute('SELECT remote,local,reason,at,count FROM failures ORDER BY remote'):
                    # Spreadsheet viewers must treat names/reasons as plain text.
                    writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row])
            os.replace(temp,path)
        except OSError:
            # CSV may be open in Excel. SQLite remains the authoritative queue.
            return False
        return True

    def failure_count(self):
        return self.db.execute("SELECT count(*) FROM files WHERE state='failed'").fetchone()[0]

    def verify_completed(self):
        for remote, size, local in self.db.execute("SELECT remote,bytes,local FROM files WHERE state='done'").fetchall():
            p = self.root / local
            if not p.resolve().is_relative_to(self.root) or not p.is_file() or p.stat().st_size != size:
                self.db.execute("UPDATE files SET state='pending' WHERE remote=?", (remote,))
        self.db.commit()

    def stats(self):
        total, done = self.db.execute("SELECT count(*),coalesce(sum(state='done'),0) FROM files").fetchone()
        folders = self.db.execute('SELECT count(*) FROM folders').fetchone()[0]
        estimate = sum((parse_size(s)[1] if parse_size(s) else 0) for (s,) in self.db.execute('SELECT size FROM files'))
        return total,done,folders,estimate

def default_downloads():
    return BASE/'Временные загрузки'

def publish_download(src, dest):
    dest.parent.mkdir(parents=True,exist_ok=True)
    if src.resolve() == dest.resolve():
        return dest
    if dest.exists():
        # Existing unrelated or modified files are never overwritten.
        stem, suffix = dest.stem, dest.suffix
        for i in range(1,100000):
            candidate = dest.with_name(f'{stem} (копия {i}){suffix}')
            if not candidate.exists():
                dest = candidate
                break
        else:
            raise Halt('Не удалось выбрать свободное имя файла.')
    if src.drive.lower() == dest.drive.lower():
        os.rename(src,dest)
    else:
        temp = dest.with_name(dest.name+'.yd-part-'+hashlib.sha256(str(src).encode()).hexdigest()[:8])
        if temp.exists():
            raise Halt('Обнаружена незавершённая локальная копия: '+str(temp))
        with src.open('rb') as inp, temp.open('xb') as out:
            shutil.copyfileobj(inp,out,4*1024**2)
            out.flush()
            os.fsync(out.fileno())
        if temp.stat().st_size != src.stat().st_size:
            raise Halt('Локальная копия неполная; исходное скачивание сохранено.')
        os.rename(temp,dest)
        src.unlink()
    return dest

def prepare_download_history(profile):
    """Work around Chromium's reused-profile download crash; keep a backup.

    Only this copier's dedicated profile is passed here. Queue state, cookies,
    browsing history and downloaded files are not changed.
    """
    history=Path(profile)/'Default'/'History'
    if not history.is_file(): return
    if os.name=='nt':
        from ctypes import wintypes
        create=ctypes.windll.kernel32.CreateFileW
        create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype=wintypes.HANDLE
        handle=create(str(history),0xC0000000,0,None,3,0x80,None)
        if handle==wintypes.HANDLE(-1).value:
            raise Halt('Закройте ранее открытый браузер программы: его профиль сейчас используется.')
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
    try:
        with closing(sqlite3.connect(history.as_uri()+'?mode=rw',uri=True,timeout=2)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            selected=[t for t in ('downloads_slices','downloads_url_chains','downloads') if t in tables]
            if not selected: return
            backup=history.with_name('History-before-copier-download-fix.sqlite3')
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as saved: db.backup(saved)
            db.execute('BEGIN IMMEDIATE')
            for table in selected: db.execute('DELETE FROM '+table)
            db.commit()
    except sqlite3.Error as e:
        raise Halt('Не удалось подготовить профиль браузера. Очередь и файлы сохранены: '+str(e)) from e

class BackgroundBrowser:
    def __init__(self,stop,log,downloads,headless=True,profile=None):
        from playwright.sync_api import sync_playwright
        self.stop,self.log=stop,log
        self.downloads,self.headless=Path(downloads),headless
        self.browser_restarts=0
        self.profile=Path(profile) if profile else Path(os.environ['LOCALAPPDATA'])/'YandexDiskCopier'/'BrowserProfile'
        self.profile.mkdir(parents=True,exist_ok=True)
        self.pw=sync_playwright().start()
        self.context=None
        try:
            prepare_download_history(self.profile.resolve())
            Path(downloads).mkdir(parents=True,exist_ok=True)
            candidates=[root/'browser-runtime'/'chrome-win64'/'chrome.exe' for root in (BASE,BASE.parent,BASE.parent.parent)]
            runtime=next((p for p in candidates if p.is_file()),candidates[0])
            browser_options={'executable_path':str(runtime)} if runtime.is_file() else {'channel':'chrome'}
            self.context=self.pw.chromium.launch_persistent_context(str(self.profile),**browser_options,
                headless=headless,accept_downloads=True,downloads_path=str(downloads),
                viewport={'width':1440,'height':1000},locale='ru-RU',timeout=60000,
                chromium_sandbox=True,args=['--restore-last-session'])
            pages=self.context.pages
            self.page=next((p for p in pages if p.url.startswith('https://disk.yandex.ru/')),
                           pages[0] if pages else None)
            if self.page is None: self.page=self.context.new_page()
            for page in list(self.context.pages):
                if page!=self.page and page.url=='about:blank': page.close()
            if not headless: self.page.bring_to_front()
            self.page.set_default_timeout(15000)
            self.page.set_default_navigation_timeout(60000)
        except Halt:
            self.close()
            raise
        except Exception:
            self.close()
            raise Halt('Не удалось запустить отдельный Chrome. Проверьте установку Chrome и закройте ранее открытый браузер этой программы. Обычный Chrome закрывать не нужно.')

    def close(self):
        try:
            if self.context: self.context.close()
        finally:
            self.pw.stop()

    def check(self):
        if ctypes.windll.user32.GetAsyncKeyState(0x77)&0x8000: self.stop.set()
        if self.stop.is_set():
            raise Halt('Остановлено. Прогресс сохранён; нажмите «Скачать / продолжить».')
        if self.page.is_closed():
            raise Halt('Отдельный браузер программы закрыт. Нажмите «Скачать / продолжить».')

    def restart_after_failure(self):
        if self.stop.is_set(): self.check()
        attempts=self.browser_restarts+1
        if attempts>3:
            raise Halt('Браузер закрылся несколько раз подряд. Автоматическое восстановление остановлено; прогресс сохранён.')
        self.close()
        self.__init__(self.stop,self.log,self.downloads,self.headless,self.profile)
        self.browser_restarts=attempts
        self.log(f'Браузер восстановлен после сбоя ({attempts}/3).')

    def remote(self):
        return remote_from_url(self.page.url)

    def login(self,source):
        self.page.goto(url_for(source),wait_until='domcontentloaded')
        self.log('Войдите в Яндекс в отдельном окне. Выберите папку и вид «Список», затем нажмите «Готово / закрыть браузер».','status')
        latest=None
        while not self.stop.is_set() and not self.page.is_closed():
            try:
                current=self.remote()
                if current!=latest and self.page.locator('.listing').count():
                    latest=current
                    self.log(current,'source')
                self.page.wait_for_timeout(400)
            except Exception:
                if self.page.is_closed(): break
                self.page.wait_for_timeout(400)
        self.log('Браузер входа закрыт. Если вход выполнен, можно сканировать и скачивать в фоне.','status')

    def wait_listing(self,parent):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        try:
            self.page.locator('.listing').wait_for(state='attached',timeout=60000)
            self.page.locator('.listing__items').wait_for(state='attached',timeout=30000)
        except BrowserTimeout:
            raise Halt('Не загрузился список файлов. Откройте видимый браузер программы и проверьте страницу, вход и выбранную папку.')
        self.check()
        self.dismiss_promo()
        if self.remote()!=parent:
            raise Halt('Яндекс перенаправил страницу. Откройте браузер программы и проверьте вход в аккаунт.')
        combo=self.page.get_by_role('combobox',name=re.compile('Вид'))
        if combo.count()==1 and 'Список' not in (combo.get_attribute('aria-label') or ''):
            combo.click()
            self.page.get_by_text('Список',exact=True).click()
            self.page.locator('.listing__items').wait_for(state='attached',timeout=30000)

    def dismiss_promo(self):
        popup=self.page.locator('[data-test-id="promopopup"][data-promo-name="yandexdcPopup"]:visible')
        if popup.count():
            close=popup.locator('[data-test-id="promopopup-CloseButton"], [aria-label="Закрыть"], [title="Закрыть"], .Modal-Close, [data-test-id="close"]')
            if close.count():
                close.first.click()
                popup.wait_for(state='hidden',timeout=5000)
                self.log('Закрыто информационное сообщение Яндекса.')
            else:
                raise Halt('Сообщение Яндекса перекрывает файлы. Закройте его крестиком в браузере программы и продолжите.')

    def navigate(self,parent):
        self.check()
        if self.page.url!=url_for(parent):
            self.page.goto(url_for(parent),wait_until='domcontentloaded')
        self.wait_listing(parent)

    def rows(self):
        return self.page.locator('.listing-item')

    @staticmethod
    def entry(row):
        values=row.evaluate('''e => {
          const title=e.querySelector('.listing-item__title');
          const col=k=>(e.querySelector('.listing-item__column_'+k)?.textContent||'').trim();
          return {name:title?.getAttribute('aria-label')||title?.textContent?.trim(),
            directory:e.classList.contains('listing-item_type_dir'),
            file:e.classList.contains('listing-item_type_file'),size:col('size'),stamp:col('date')+' '+col('time')};
        }''')
        if not values['name'] or not (values['directory'] or values['file']):
            raise Halt('Изменился интерфейс Диска: тип или имя объекта не распознаны.')
        return Entry(values['name'],values['directory'],values['size'],values['stamp'])

    def reset_scroll(self):
        rows=self.rows()
        if rows.count(): rows.first.scroll_into_view_if_needed()
        self.page.evaluate('window.scrollTo(0,0)')
        self.page.wait_for_timeout(150)

    def scroll_next(self):
        rows=self.rows()
        if rows.count(): rows.last.scroll_into_view_if_needed()
        self.page.mouse.move(1100,750)
        self.page.mouse.wheel(0,700)
        self.page.wait_for_timeout(600)
        self.check()

    def signature(self):
        return self.rows().evaluate_all('es => es.map(e=>[e.querySelector(".listing-item__title")?.getAttribute("aria-label"),Math.round(e.getBoundingClientRect().top)])')

    def scan_folder(self,parent):
        self.navigate(parent)
        self.reset_scroll()
        found,previous,quiet={},None,0
        for _ in range(100000):
            self.check()
            values=self.rows().evaluate_all('''es => es.map(e => {
              const title=e.querySelector('.listing-item__title');
              const col=k=>(e.querySelector('.listing-item__column_'+k)?.textContent||'').trim();
              return {name:title?.getAttribute('aria-label')||title?.textContent?.trim(),
                directory:e.classList.contains('listing-item_type_dir'),
                file:e.classList.contains('listing-item_type_file'),size:col('size'),stamp:col('date')+' '+col('time')};
            })''')
            for value in values:
                if not value['name'] or not (value['directory'] or value['file']):
                    raise Halt('Изменился интерфейс Диска: тип или имя объекта не распознаны.')
                entry=Entry(value['name'],value['directory'],value['size'],value['stamp'])
                found[entry.name]=entry
            signature=self.signature()
            quiet=quiet+1 if signature==previous else 0
            if quiet>=5 and self.page.locator('.listing__items').count():
                return list(found.values())
            previous=signature
            self.scroll_next()
        raise Halt('Слишком большой список: сканирование остановлено без отметки завершения.')

    def find_row(self,parent,name):
        self.navigate(parent)
        self.reset_scroll()
        previous,quiet=None,0
        for _ in range(100000):
            self.check()
            match=self.rows().filter(has=self.page.get_by_label(name,exact=True))
            if match.count()==1:
                match.scroll_into_view_if_needed()
                return match
            signature=self.signature()
            quiet=quiet+1 if signature==previous else 0
            if quiet>=5: raise FileFailure('Файл исчез или список изменился: '+name)
            previous=signature
            self.scroll_next()
        raise Halt('Не удалось закончить поиск файла.')

    def download_file(self,parent,name):
        download=self.start_download(parent,name)
        return self.finish_download(download,name)

    def select_download(self,parent,name):
        from playwright.sync_api import TimeoutError as BrowserTimeout,Error as BrowserError
        row=self.find_row(parent,name)
        self.dismiss_promo()
        try:
            row.click()
        except BrowserTimeout:
            self.dismiss_promo()
            row.click()
        selected=self.page.locator('.listing-item[aria-selected="true"]')
        if selected.count()!=1 or self.entry(selected).name!=name or self.entry(selected).directory:
            raise Halt('Выделен не один файл. Скачивание остановлено.')
        self.check()

    def fork_page(self):
        child=copy.copy(self)
        child.page=self.context.new_page()
        child.page.set_default_timeout(15000)
        child.page.set_default_navigation_timeout(60000)
        child.batch_slot=True
        child.owned_pages=[child.page]
        return child

    def close_slot(self):
        for page in getattr(self,'owned_pages',[]):
            if not page.is_closed(): page.close()

    def start_download(self,parent,name):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        self.select_download(parent,name)
        downloads,server_errors,watched=[],[],[]
        def on_download(download): downloads.append(download)
        def watch_page(page):
            if getattr(self,'batch_slot',False) and page!=self.page and page.opener() not in watched: return
            watched.append(page)
            if getattr(self,'batch_slot',False) and page not in self.owned_pages: self.owned_pages.append(page)
            page.on('download',on_download)
        def on_response(response):
            if getattr(self,'batch_slot',False):
                try:
                    if response.frame.page not in watched: return
                except Exception: return
            host=urlparse(response.url).hostname or ''
            if (host=='downloader.disk.yandex.ru' or host.endswith('.downloader.disk.yandex.ru')) and response.status>=400:
                server_errors.append(response.status)
        for page in ([self.page] if getattr(self,'batch_slot',False) else self.context.pages): watch_page(page)
        self.context.on('page',watch_page)
        self.context.on('response',on_response)
        download=None
        try:
            self.page.get_by_role('button',name='Скачать',exact=True).click(no_wait_after=True)
            deadline=time.monotonic()+45
            while not downloads:
                self.check()
                if server_errors:
                    raise FileFailure(f'Сервер загрузки Яндекса вернул HTTP {server_errors[-1]}. Файл сохранён в очереди для повторной попытки.')
                if time.monotonic()>=deadline:
                    break
                self.page.wait_for_timeout(250)
            if downloads: download=downloads[0]
        except BrowserTimeout:
            raise Halt('Не удалось нажать кнопку скачивания. Проверьте сообщения в браузере программы.')
        finally:
            self.context.remove_listener('page',watch_page)
            self.context.remove_listener('response',on_response)
            for page in watched: page.remove_listener('download',on_download)
        if download is None:
            # Abort any delayed transfer before proceeding, so its event or
            # artifact cannot be mistaken for the next file's download.
            if getattr(self,'batch_slot',False):
                self.close_slot()
                raise FileFailure('Яндекс не начал скачивание за 45 секунд. Файл сохранён для повторной попытки.')
            self.close()
            self.__init__(self.stop,self.log,self.downloads,self.headless,self.profile)
            raise FileFailure('Яндекс не начал скачивание за 45 секунд. Браузер перезапущен; файл сохранён для повторной попытки.')
        return download

    def finish_download(self,download,name):
        from playwright.sync_api import Error as BrowserError
        try:
            # The browser associates this artifact with the exact click. No
            # filename guessing or monitoring of the user's Downloads folder.
            failure=download.failure()
            if failure:
                if self.page.is_closed() and not self.stop.is_set() and not getattr(self,'batch_slot',False):
                    self.restart_after_failure()
                    raise FileFailure('Браузер закрылся во время скачивания и восстановлен. Файл оставлен для повторной попытки.')
                if not getattr(self,'batch_slot',False): self.check()
                raise FileFailure('Chrome не завершил скачивание: '+str(failure))
            path=download.path()
        except BrowserError:
            if self.page.is_closed() and not self.stop.is_set() and not getattr(self,'batch_slot',False):
                self.restart_after_failure()
                raise FileFailure('Браузер закрылся во время скачивания и восстановлен. Файл оставлен для повторной попытки.')
            raise Halt('Браузер прервал скачивание. Прогресс очереди сохранён.')
        if failure or path is None:
            raise FileFailure('Chrome не завершил скачивание: '+str(failure or 'файл отсутствует'))
        expected=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).rstrip(' .')
        if download.suggested_filename not in (name,expected):
            artifact=Path(path)
            if artifact.parent.resolve()==self.downloads.resolve(): artifact.unlink(missing_ok=True)
            raise FileFailure('Яндекс выдал другое имя файла: '+download.suggested_filename+'; ожидалось '+name+'. Файл не сохранён как нужный.')
        self.browser_restarts=0
        return Path(path)

class PublicBrowser(BackgroundBrowser):
    """Public website pages, including photo grids; no public REST API."""
    def configure_public(self,link):
        self.public_link=public_url(link)
        self.public_root=source_identity('',self.public_link)

    def public_path(self,remote):
        if remote!=self.public_root and not remote.startswith(self.public_root+'/'):
            raise Halt('Путь не принадлежит выбранной публичной ссылке.')
        return remote[len(self.public_root):].strip('/')

    def public_page_url(self,remote):
        suffix=self.public_path(remote)
        return self.public_link+('/'+quote(suffix,safe='/') if suffix else '')

    def page_resource(self):
        # The public page embeds its current resource for rendering. Return only
        # file metadata, never account data or expiring download URLs.
        return self.page.evaluate('''() => {
          const el=document.getElementById('store-prefetch');
          if(!el) return null;
          const s=JSON.parse(el.textContent);
          const tail=decodeURIComponent(location.pathname.split('/').slice(3).join('/'));
          const candidate=Object.values(s.resources||{}).find(r=>r.type==='file' && r.path?.endsWith(':/'+tail));
          const r=candidate || s.resources?.[s.currentResourceId];
          return r ? {name:r.name,type:r.type,size:r.meta?.size,stamp:String(r.modified||'')} : null;
        }''')

    def navigate(self,parent):
        self.check()
        expected=self.public_page_url(parent)
        try: current=public_url(self.page.url)
        except Halt: current=''
        if current != expected:
            self.page.goto(expected,wait_until='domcontentloaded')
            self.page.wait_for_timeout(1200)  # Public controls hydrate after server HTML.
        self.page.locator('#store-prefetch, .listing').first.wait_for(state='attached',timeout=60000)
        resource=self.page_resource()
        if resource and resource['type']=='file': return
        combo=self.page.get_by_role('combobox',name=re.compile('Вид'))
        if combo.count()==1 and 'Список' not in (combo.get_attribute('aria-label') or ''):
            combo.click();self.page.get_by_text('Список',exact=True).click()
        self.page.locator('.listing__items').wait_for(state='attached',timeout=30000)
        self.dismiss_promo()
        if public_url(self.page.url)!=expected:
            raise Halt('Публичная страница перенаправлена. Проверьте ссылку в браузере программы.')

    def scan_folder(self,parent):
        self.navigate(parent)
        resource=self.page_resource()
        if resource and resource['type']=='file':
            if not isinstance(resource.get('size'),int): raise Halt('На публичной странице нет размера файла.')
            return [Entry(resource['name'],False,str(resource['size'])+' Б',resource['stamp'])]
        entries=super().scan_folder(parent)
        metadata=self.page.evaluate('''() => {
          const el=document.getElementById('store-prefetch');if(!el) return [];
          const s=JSON.parse(el.textContent);
          return Object.values(s.resources||{}).filter(r=>r.parent===s.currentResourceId && r.type==='file')
            .map(r=>({name:r.name,size:r.meta?.size,stamp:String(r.modified||'')}));
        }''')
        by_name={r['name']:r for r in metadata}
        for e in entries:
            item=by_name.get(e.name)
            if not e.directory and item and isinstance(item.get('size'),int):
                e.size=str(item['size'])+' Б';e.stamp=item['stamp']
        return entries

    def find_row(self,parent,name):
        self.navigate(parent)
        resource=self.page_resource()
        if resource and resource['type']=='file':
            if resource['name']!=name: raise FileFailure('Публичный файл изменился: '+name)
            return Entry(name,False,str(resource['size'])+' Б',resource['stamp'])
        return super().find_row(parent,name)

    @staticmethod
    def entry(row):
        if isinstance(row,Entry): return row
        result=BackgroundBrowser.entry(row)
        item=row.evaluate('''e => {
          const el=document.getElementById('store-prefetch');if(!el) return null;
          const s=JSON.parse(el.textContent),name=e.querySelector('.listing-item__title')?.getAttribute('aria-label');
          const r=Object.values(s.resources||{}).find(r=>r.parent===s.currentResourceId && r.name===name && r.type==='file');
          return r ? {size:r.meta?.size,stamp:String(r.modified||'')} : null;
        }''')
        if item and isinstance(item.get('size'),int):result.size=str(item['size'])+' Б';result.stamp=item['stamp']
        return result

    def select_download(self,parent,name):
        row=self.find_row(parent,name)
        if isinstance(row,Entry):
            self.check();return
        super().select_download(parent,name)

    def login(self,source):
        self.page.goto(self.public_link,wait_until='domcontentloaded')
        self.log('Публичная ссылка открыта. При необходимости выполните проверку самостоятельно, затем нажмите «Готово / закрыть браузер».','status')
        while not self.stop.is_set() and not self.page.is_closed(): self.page.wait_for_timeout(400)

class Runner:
    def __init__(self, source, target, downloads, stop, log, policy=None,headless=True,parallel=1,public_link='',create_subfolder=False):
        self.public_link=public_url(public_link)
        self.source = source_identity(source,self.public_link)
        self.target, self.downloads = Path(target),Path(downloads)
        self.target_base=self.target
        self.create_subfolder=create_subfolder
        self.stop,self.log = stop,log
        self.policy = policy or ErrorPolicy()
        self.headless=headless
        self.parallel=int(parallel)
        if self.parallel not in PARALLEL_COUNTS: raise Halt('Выберите 1, 5, 10 или 50 одновременных загрузок.')

    def download_one(self, ui, cat, record):
        dest,bounds=self.prepare_file(ui,cat,record)
        src=ui.download_file(record[1],record[2])
        self.save_file(cat,record,src,dest,bounds)

    def prepare_file(self,ui,cat,record):
        remote,parent,name,size,stamp=record
        ui.check()
        bounds=parse_size(size)
        if bounds is None:
            raise FileFailure('Не удалось прочитать размер файла: '+name+'. Выберите вид «Список».')
        required=bounds[1]+RESERVE
        dest=cat.path(remote)
        dest.parent.mkdir(parents=True,exist_ok=True)
        for p in (self.downloads,dest.parent):
            if shutil.disk_usage(p).free < required:
                raise Halt(f'Недостаточно места на {p.drive}: требуется минимум {required/1024**3:.1f} ГБ для следующего файла. Прогресс сохранён.')
        ui.navigate(parent)
        row=ui.find_row(parent,name)
        current=ui.entry(row)
        if current.directory or (current.size,current.stamp)!=(size,stamp):
            raise FileFailure('Файл на Диске изменился после сканирования: '+name+'. Нажмите «Пересканировать».')
        self.log('Скачивание: '+remote,'status')
        return dest,bounds

    def save_file(self,cat,record,src,dest,bounds):
        remote,parent,name,size,stamp=record
        if not bounds[0]<=src.stat().st_size<=bounds[1]:
            if src.parent.resolve()==self.downloads.resolve(): src.unlink(missing_ok=True)
            raise FileFailure('Размер скачанного файла отличается от размера на Диске: '+name)
        actual=publish_download(src,dest)
        cat.done(remote,actual)
        self.log('Сохранён: '+str(actual))
        self.log(cat.stats(),'stats')
        self.log(cat.failure_count(),'failures')

    def process_files(self, ui, cat):
        records=cat.db.execute("SELECT remote,parent,name,size,stamp FROM files WHERE state IN ('pending','failed') ORDER BY parent,name").fetchall()
        if self.parallel>1:
            return self.process_batches(ui,cat,records)
        for record in records:
            failures=0
            while True:
                ui.check()
                try:
                    self.download_one(ui,cat,record)
                    break
                except FileFailure as e:
                    failures+=1
                    if not cat.failed(record[0],e):
                        self.log('CSV сейчас нельзя обновить (возможно, открыт в Excel). Ошибка сохранена в базе очереди.')
                    self.log(f'Не скачан: {record[0]} — {e}')
                    self.log(cat.failure_count(),'failures')
                    ui.check()
                    action=self.policy.decide(record[0],str(e),failures)
                    if action=='stop':
                        raise Halt('Очередь остановлена. Не скачанные файлы сохранены; нажмите «Скачать / продолжить».')
                    if action=='skip':
                        self.log('Пропущен до следующего запуска: '+record[0])
                        break
                    self.log('Повторная попытка: '+record[0])
                except (Halt,OSError) as e:
                    # Global interruption cannot safely be skipped (active download,
                    # lack of space, lost tab or local write failure).
                    if not self.stop.is_set():
                        cat.failed(record[0],e)
                        self.log(f'Остановка на файле: {record[0]} — {e}')
                        self.log(cat.failure_count(),'failures')
                    raise

    def process_batches(self,ui,cat,records):
        from playwright.sync_api import Error as BrowserError
        attempts={}
        remaining=list(records)
        while remaining:
            ui.check()
            batch,remaining=remaining[:self.parallel],remaining[self.parallel:]
            required=sum((parse_size(r[3]) or (0,0))[1] for r in batch)+RESERVE
            for p in (self.downloads,cat.root):
                if shutil.disk_usage(p).free<required: raise Halt('Недостаточно места для выбранного числа одновременных загрузок. Уменьшите число файлов или выберите другой диск.')
            active,problems=[],[]
            halt=None
            for record in batch:
                if self.stop.is_set(): break
                slot=None
                try:
                    slot=ui.fork_page()
                    dest,bounds=self.prepare_file(slot,cat,record)
                    download=slot.start_download(record[1],record[2])
                    active.append((slot,record,download,dest,bounds))
                    self.log(f'Запущено в группе: {len(active)} / {self.parallel}','status')
                except FileFailure as e:
                    problems.append((record,e))
                    if slot:
                        try: slot.close_slot()
                        except BrowserError:
                            halt=Halt('Браузер прервал группу. Очередь сохранена.');break
                except (Halt,OSError,BrowserError) as e:
                    if not self.stop.is_set(): cat.failed(record[0],e)
                    halt=Halt('Браузер прервал запуск группы. Очередь сохранена: '+str(e)) if isinstance(e,BrowserError) else e
                    if slot:
                        try: slot.close_slot()
                        except BrowserError: pass
                    break
            # Chrome transfers all started artifacts concurrently. Drain the
            # group before honoring pause or asking about per-file failures.
            for slot,record,download,dest,bounds in active:
                try:
                    src=slot.finish_download(download,record[2])
                    self.save_file(cat,record,src,dest,bounds)
                except FileFailure as e: problems.append((record,e))
                except (Halt,OSError) as e:
                    cat.failed(record[0],e);halt=e
                finally:
                    try: slot.close_slot()
                    except BrowserError: pass
            for record,error in problems:
                cat.failed(record[0],error)
                self.log(f'Не скачан: {record[0]} — {error}')
                self.log(cat.failure_count(),'failures')
            if halt: raise halt
            ui.check()
            for record,error in problems:
                count=attempts.get(record[0],0)+1;attempts[record[0]]=count
                action=self.policy.decide(record[0],str(error),count)
                if action=='stop': raise Halt('Очередь остановлена. Прогресс сохранён; нажмите «Скачать / продолжить».')
                if action=='retry': remaining.insert(0,record)
                else: self.log('Пропущен до следующего запуска: '+record[0])

    def run(self,mode='download'):
        self.downloads.mkdir(parents=True,exist_ok=True)
        cls=PublicBrowser if self.public_link else BackgroundBrowser
        ui=cls(self.stop,self.log,self.downloads,headless=False if mode=='login' else self.headless)
        if self.public_link: ui.configure_public(self.public_link)
        try:
            if mode=='login':
                ui.login(self.source)
                return
            self.run_queue(ui,mode)
        finally:
            ui.close()

    def run_queue(self,ui,mode):
        self.target=self.target_base
        if self.create_subfolder:
            if self.public_link:
                ui.navigate(self.source)
                resource=ui.page_resource()
                if not resource or not resource.get('name'): raise Halt('Не удалось прочитать название публичного файла или папки.')
                name=Path(resource['name']).stem if resource['type']=='file' else resource['name']
            else: name=self.source.rsplit('/',1)[-1] or 'Яндекс Диск'
            self.target=self.target_base/safe_name(name)
        self.log(str(self.target),'target_path')
        cat = Catalog(self.target,self.source)
        try:
            if mode=='rescan':
                cat.db.execute("UPDATE folders SET state='pending'");cat.db.commit()
            while True:
                pending = cat.db.execute("SELECT remote FROM folders WHERE state='pending' ORDER BY remote LIMIT 1").fetchone()
                if not pending:
                    break
                parent = pending[0]
                self.log('Сканирование: /'+parent,'status')
                entries = ui.scan_folder(parent)
                cat.record(parent,entries)
                self.log(f'Папка /{parent or ""}: {len(entries)} объектов')
                self.log(cat.stats(),'stats')
            cat.verify_completed()
            cat.export_failures()
            self.log(cat.failure_count(),'failures')
            self.log(cat.stats(),'stats')
            if mode in ('scan','rescan'):
                self.log('Сканирование завершено. Структура папок создана; можно начать скачивание.','status')
                return
            self.log(f'Одновременных загрузок: {self.parallel}. Пауза — после завершения начатой группы, кнопка или F8.')
            self.process_files(ui,cat)
            ui.navigate(self.source)
            failed=cat.failure_count()
            if failed:
                self.log(f'Проход завершён. Не скачано: {failed}. Список — «Не скачано.csv»; повторить — «Скачать / продолжить».','status')
            else:
                self.log('Готово. Все файлы из сохранённой очереди скачаны.','status')
        finally:
            cat.close()

def main():
    from interface import show
    show(sys.modules[__name__])

if __name__=='__main__':
    if '--self-test' in sys.argv:
        # Validate frozen dependencies without controlling any browser/window.
        import tkinter
        import tempfile
        with tempfile.TemporaryDirectory() as task_temp:
            engine=BackgroundBrowser(threading.Event(),lambda *v:None,Path(task_temp)/'downloads',profile=Path(task_temp)/'profile')
            chrome_version=engine.context.browser.version if engine.context.browser else 'persistent'
            engine.close()
        report={'result':'ok','python':sys.version.split()[0],'tk':tkinter.Tcl().eval('info patchlevel'),
                'background_chrome':chrome_version,'sqlite':sqlite3.sqlite_version}
        (BASE/'self-test.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    else:
        main()
