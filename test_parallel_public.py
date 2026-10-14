import html
import tempfile
import threading
import time
import json
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from downloader import (BackgroundBrowser,PublicBrowser,Runner,Catalog,Entry,
                        ErrorPolicy,FileFailure,Halt,public_url,source_identity)

class QueuedBrowser:
    def __init__(self,root,stop,events,bad=()):
        self.root,self.stop,self.events,self.bad=root,stop,events,set(bad)
    def check(self):
        if self.stop.is_set(): raise Halt('pause')
    def navigate(self,parent): pass
    def find_row(self,parent,name): return Entry(name,False,'1024 Б','stamp')
    def entry(self,row): return row
    def fork_page(self): return self
    def close_slot(self): pass
    def start_download(self,parent,name):
        self.check();self.events.append(('start',name));return name
    def finish_download(self,event,name):
        self.events.append(('finish',name))
        if name in self.bad: raise FileFailure('bad payload')
        p=self.root/name;p.write_bytes(b'x'*1024);return p

class ParallelPublicTests(unittest.TestCase):
    def test_public_single_file_and_nested_folder_browser(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);stop=threading.Event();link='https://disk.yandex.ru/d/fixture'
            ui=PublicBrowser(stop,lambda *x:None,root/'stage',profile=root/'profile');ui.configure_public(link)
            def route_page(route):
                path=route.request.url.split('/d/fixture')[-1]
                if route.request.url.endswith('/payload'):
                    route.fulfill(status=200,body=b'x'*1024,headers={'Content-Disposition':'attachment; filename="one.txt"'});return
                if path.startswith('/folder'):
                    resource={'id':'file','name':'one.txt','type':'file','modified':7,'meta':{'size':1024}}
                    state={'currentResourceId':'file','resources':{'file':resource}}
                    body='<button onclick="location.href=\'/payload\'">Скачать</button>'
                else:
                    state={'currentResourceId':'root','resources':{'root':{'name':'root','type':'dir','id':'root'}}}
                    body='<div class="listing"><div class="listing__items"><div class="listing-item listing-item_type_dir"><div class="listing-item__title" aria-label="folder">folder</div></div></div></div>'
                body+='<script id="store-prefetch" type="application/json">'+json.dumps(state)+'</script>'
                route.fulfill(status=200,body=body,content_type='text/html; charset=utf-8')
            ui.context.route('**/*',route_page)
            try:
                runner=Runner('',root/'copy',root/'stage',stop,lambda *x:None,ErrorPolicy(True),public_link=link,parallel=5,create_subfolder=True)
                runner.run_queue(ui,'download')
                self.assertEqual((root/'copy/root/folder/one.txt').read_bytes(),b'x'*1024)
                c=Catalog(root/'copy/root',source_identity('',link));self.assertEqual(c.stats()[:3],(1,1,2));c.close()
                # A URL to a standalone file also yields exactly one file.
                ui.configure_public(link+'/folder')
                single=Runner('',root/'single',root/'stage',stop,lambda *x:None,ErrorPolicy(True),public_link=link+'/folder',create_subfolder=True)
                single.run_queue(ui,'download')
                self.assertEqual((root/'single/one/one.txt').stat().st_size,1024)
            finally:ui.close()

    def test_public_url_and_source_isolation(self):
        link='https://disk.yandex.ru/d/abc/Папка?utm_source=test'
        self.assertEqual(public_url(link),'https://disk.yandex.ru/d/abc/%D0%9F%D0%B0%D0%BF%D0%BA%D0%B0')
        self.assertEqual(public_url('https://disk.360.yandex.ru/d/abc'),public_url('https://yadi.sk/d/abc'))
        for bad in ('https://evil.test/d/abc','https://disk.yandex.ru/client/disk','https://disk.yandex.ru.evil/d/abc','https://a@disk.yandex.ru/d/abc'):
            with self.assertRaises(Halt): public_url(bad)
        with tempfile.TemporaryDirectory() as t:
            c=Catalog(t,source_identity('',link));c.close()
            with self.assertRaises(Halt): Catalog(t,source_identity('','https://disk.yandex.ru/d/other'))

    def test_limits_resume_and_failure_in_original_folder(self):
        for count in (5,10,50):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as t:
                root=Path(t);stage=root/'stage';stage.mkdir();events=[];stop=threading.Event()
                c=Catalog(root/'copy','');c.record('',[Entry('nested',True,'','')])
                c.record('nested',[Entry(f'{i:03}.bin',False,'1024 Б','stamp') for i in range(count+2)])
                ui=QueuedBrowser(stage,stop,events,bad=['001.bin'])
                runner=Runner('',c.root,stage,stop,lambda *x:None,ErrorPolicy(True),parallel=count)
                runner.process_files(ui,c)
                self.assertEqual([k for k,n in events[:count]],['start']*count)
                self.assertEqual(c.failure_count(),1)
                self.assertEqual(c.stats()[1],count+1)
                events.clear();ui.bad.clear();runner.process_files(ui,c)
                self.assertEqual(events,[('start','001.bin'),('finish','001.bin')])
                self.assertEqual((c.root/'nested/001.bin').stat().st_size,1024)
                self.assertEqual(c.failure_count(),0);c.close()

    def test_pause_drains_started_files(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);stage=root/'stage';stage.mkdir();events=[];stop=threading.Event()
            c=Catalog(root/'copy','');c.record('',[Entry(f'{i}.bin',False,'1024 Б','stamp') for i in range(8)])
            ui=QueuedBrowser(stage,stop,events)
            original=ui.start_download
            def start(parent,name):
                result=original(parent,name)
                if len(events)==3:stop.set()
                return result
            ui.start_download=start
            runner=Runner('',c.root,stage,stop,lambda *x:None,ErrorPolicy(True),parallel=5)
            with self.assertRaises(Halt):runner.process_files(ui,c)
            self.assertEqual(c.stats()[1],3)
            self.assertEqual(c.failure_count(),0);c.close()

    def test_browser_error_drains_started_files_before_stopping(self):
        from playwright.sync_api import Error as BrowserError
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);stage=root/'stage';stage.mkdir();events=[];stop=threading.Event()
            c=Catalog(root/'copy','');c.record('',[Entry(f'{i}.bin',False,'1024 Б','stamp') for i in range(3)])
            ui=QueuedBrowser(stage,stop,events);created=[0]
            def fork():
                created[0]+=1
                if created[0]==2:raise BrowserError('new page unavailable')
                return ui
            ui.fork_page=fork
            with self.assertRaisesRegex(Halt,'Очередь сохранена'):
                Runner('',c.root,stage,stop,lambda *x:None,ErrorPolicy(True),parallel=5).process_files(ui,c)
            self.assertEqual(events,[('start','0.bin'),('finish','0.bin')])
            self.assertEqual(c.stats()[1],1);self.assertEqual(c.failure_count(),1);c.close()

    def test_cleanup_error_after_failed_start_still_drains_group(self):
        from playwright.sync_api import Error as BrowserError
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);stage=root/'stage';stage.mkdir();events=[];stop=threading.Event()
            c=Catalog(root/'copy','');c.record('',[Entry(f'{i}.bin',False,'1024 Б','stamp') for i in range(3)])
            ui=QueuedBrowser(stage,stop,events);original=ui.start_download
            def start(parent,name):
                if name=='1.bin':raise FileFailure('HTTP 500')
                return original(parent,name)
            def close():raise BrowserError('connection lost on close')
            ui.start_download=start;ui.close_slot=close
            with self.assertRaisesRegex(Halt,'Очередь сохранена'):
                Runner('',c.root,stage,stop,lambda *x:None,ErrorPolicy(True),parallel=5).process_files(ui,c)
            self.assertEqual(events,[('start','0.bin'),('finish','0.bin')])
            self.assertEqual(c.stats()[1],1);self.assertEqual(c.failure_count(),1);c.close()

    def test_real_browser_transfers_overlap(self):
        active=0;peak=0;lock=threading.Lock();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*x):pass
            def do_GET(self):
                nonlocal active,peak
                if self.path.startswith('/download/'):
                    name=self.path.rsplit('/',1)[-1]
                    self.send_response(200);self.send_header('Content-Length','1024')
                    self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Disposition',f'attachment; filename="{name}"');self.end_headers()
                    self.wfile.write(b'x'*512);self.wfile.flush()
                    with lock:
                        active+=1;peak=max(peak,active)
                        if active==5:release.set()
                    try:
                        if not release.wait(30):return
                        self.wfile.write(b'x'*512)
                    finally:
                        with lock:active-=1
                else:
                    rows=''.join(f'<div class="listing-item listing-item_type_file" aria-selected="false" onclick="pick(this)"><div class="listing-item__title" aria-label="{i}.bin">{i}.bin</div><div class="listing-item__column_size">1024 Б</div><div class="listing-item__column_date">stamp</div></div>' for i in range(5))
                    body='<button onclick="location.href=\'/download/\'+fileName">Скачать</button><div class="listing"><div class="listing__items">'+rows+'</div></div><script>let fileName;function pick(el){document.querySelectorAll(".listing-item").forEach(e=>e.setAttribute("aria-selected","false"));el.setAttribute("aria-selected","true");fileName=el.querySelector(".listing-item__title").textContent}</script>'
                    self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body.encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        class LocalBrowser(BackgroundBrowser):
            def navigate(self,parent):
                self.check()
                url=f'http://127.0.0.1:{server.server_port}/'
                if self.page.url!=url:self.page.goto(url)
        try:
            with tempfile.TemporaryDirectory() as t:
                root=Path(t);stop=threading.Event()
                ui=LocalBrowser(stop,lambda *x:None,root/'stage',profile=root/'profile')
                c=Catalog(root/'copy','');c.record('',[Entry(f'{i}.bin',False,'1024 Б','stamp ') for i in range(5)])
                try:
                    Runner('',c.root,root/'stage',stop,lambda *x:None,ErrorPolicy(True),parallel=5).process_files(ui,c)
                    self.assertEqual(peak,5,'Five browser downloads must be active before any completes')
                    self.assertEqual(c.stats()[1],5,c.db.execute('SELECT remote,reason FROM failures').fetchall())
                    for i in range(5):self.assertEqual((c.root/f'{i}.bin').read_bytes(),b'x'*1024)
                finally:release.set();c.close();ui.close()
        finally:release.set();server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
