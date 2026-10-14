"""Full browser pipeline, intercepted fixture pages; no Yandex account/network."""
import html
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import unquote,urlparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from downloader import BackgroundBrowser,Runner,Catalog,ErrorPolicy,url_for,FileFailure

TREE={
    '':[('Папка',True),('Пустая',True),('root.txt',False)],
    'Папка':[('Вложенная',True),('one.txt',False)],
    'Папка/Вложенная':[('two.txt',False)],
    'Пустая':[],
}

def fixture_page(remote):
    rows=[]
    for name,directory in TREE[remote]:
        cls='dir' if directory else 'file'
        size='' if directory else '1 КБ'
        rows.append(f'''<div role="row" aria-selected="false" tabindex="0" onclick="select(this)"
            class="listing-item listing-item_type_{cls}">
            <div class="listing-item__title" aria-label="{html.escape(name,quote=True)}">{html.escape(name)}</div>
            <div class="listing-item__column_size">{size}</div>
            <div class="listing-item__column_date">11.10.2026</div>
            <div class="listing-item__column_time">12:00</div></div>''')
    return '''<!doctype html><html><meta charset="utf-8"><style>.listing-item{height:60px}</style>
        <button onclick="downloadSelected()">Скачать</button><div class="listing"><div class="listing__items">'''+''.join(rows)+'''</div></div><script>
        let selected;
        function select(el){document.querySelectorAll('.listing-item').forEach(e=>e.setAttribute('aria-selected','false'));
            el.setAttribute('aria-selected','true');selected=el.querySelector('.listing-item__title').getAttribute('aria-label');}
        function downloadSelected(){location.href='/fixture-download/'+encodeURIComponent(selected);}
        </script></html>'''

class BrowserPipelineTest(unittest.TestCase):
    def test_large_download_after_profile_restart(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                if self.path=='/file':
                    self.send_response(200)
                    self.send_header('Content-Length',str(20*1024*1024))
                    self.send_header('Content-Disposition','attachment; filename="fixture.bin"')
                    self.end_headers()
                    for _ in range(20):self.wfile.write(b'x'*1024*1024)
                else:
                    self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
                    self.wfile.write(b'<a href="/file">Download</a>')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                for _ in range(3):
                    ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
                    try:
                        ui.page.goto(f'http://127.0.0.1:{server.server_port}')
                        with ui.page.expect_download(timeout=10000) as event:
                            ui.page.get_by_text('Download',exact=True).click()
                        self.assertIsNone(event.value.failure())
                        self.assertEqual(event.value.path().stat().st_size,20*1024*1024)
                    finally:ui.close()
                self.assertTrue((root/'profile'/'Default'/'History-before-copier-download-fix.sqlite3').is_file())
        finally:server.shutdown();server.server_close()

    def test_closed_browser_recovery_keeps_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
            try:
                ui.context.add_cookies([{'name':'fixture_login','value':'saved','url':'https://disk.yandex.ru'}])
                ui.close()
                ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
                ui.page.close()
                ui.restart_after_failure()
                self.assertFalse(ui.page.is_closed())
                self.assertEqual(ui.browser_restarts,1)
                self.assertIn(('fixture_login','saved'),[(c['name'],c['value']) for c in ui.context.cookies('https://disk.yandex.ru')])
            finally:ui.close()

    def test_server_failure_does_not_stop_next_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
            failed=[True]
            filename=['other.txt']
            def route_fixture(route):
                u=urlparse(route.request.url)
                if u.path.startswith('/client/disk'):
                    page=fixture_page('Папка').replace("location.href='/fixture-download/'", "location.href='https://downloader.disk.yandex.ru/fixture-download/'")
                    route.fulfill(status=200,body=page,content_type='text/html; charset=utf-8')
                elif u.path.startswith('/fixture-download/'):
                    if failed[0]: route.fulfill(status=500,body='temporary server error')
                    else: route.fulfill(status=200,body=b'x'*1024,headers={'Content-Disposition':'attachment; filename="'+filename[0]+'"'})
                else: route.fulfill(status=404,body='fixture')
            ui.context.route('**/*',route_fixture)
            try:
                with self.assertRaisesRegex(FileFailure,'HTTP 500'):
                    ui.download_file('Папка','one.txt')
                failed[0]=False
                with self.assertRaisesRegex(FileFailure,'другое имя файла'):
                    ui.download_file('Папка','one.txt')
                filename[0]='one.txt'
                self.assertEqual(ui.download_file('Папка','one.txt').stat().st_size,1024)
            finally: ui.close()

    def test_login_profile_survives_browser_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
            ui.context.add_cookies([{'name':'fixture_login','value':'saved','url':'https://disk.yandex.ru'}])
            ui.close()
            ui=BackgroundBrowser(threading.Event(),lambda *v:None,root/'stage',profile=root/'profile')
            try:
                cookies=ui.context.cookies('https://disk.yandex.ru')
                self.assertIn(('fixture_login','saved'),[(c['name'],c['value']) for c in cookies])
            finally: ui.close()

    def test_nested_tree_downloads_background_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); events=[]
            def log(value,kind='log'): events.append((kind,str(value)))
            ui=BackgroundBrowser(threading.Event(),log,root/'stage',profile=root/'profile')
            download_events=[]
            ui.page.on('download',lambda d:download_events.append(d.suggested_filename))
            def route_fixture(route):
                u=urlparse(route.request.url)
                if u.path.startswith('/fixture-download/'):
                    name=unquote(u.path.split('/')[-1])
                    route.fulfill(status=200,body=(name[0].encode()*1024),content_type='application/octet-stream',
                                  headers={'Content-Disposition':'attachment; filename="'+name+'"'})
                elif u.path.startswith('/client/disk'):
                    remote=unquote(u.path[len('/client/disk'):]).strip('/')
                    body=fixture_page(remote).replace('<script>', '''<div data-test-id="promopopup" data-promo-name="yandexdcPopup" style="position:fixed;inset:0;z-index:999;background:white">
                    <button data-test-id="promopopup-CloseButton" aria-label="" onclick="this.parentElement.remove()">×</button></div><script>''')
                    route.fulfill(status=200,body=body,content_type='text/html; charset=utf-8')
                else:
                    route.fulfill(status=404,body='fixture only')
            ui.context.route('**/*',route_fixture)
            runner=Runner('',root/'copy',root/'stage',threading.Event(),log,ErrorPolicy(True))
            try:
                runner.run_queue(ui,'download')
                self.assertTrue((root/'copy'/'Пустая').is_dir())
                for remote in ['root.txt','Папка/one.txt','Папка/Вложенная/two.txt']:
                    self.assertEqual((root/'copy'/remote).stat().st_size,1024)
                self.assertEqual(len(download_events),3)
                c=Catalog(root/'copy','')
                self.assertEqual(c.stats()[:3],(3,3,4))
                self.assertEqual(c.failure_count(),0); c.close()
                phases=[value for kind,value in events if value.startswith(('Скачивание:','Сохранён:'))]
                self.assertEqual([s.split(':')[0] for s in phases],['Скачивание','Сохранён']*3)
                runner.run_queue(ui,'download')
                self.assertEqual(len(download_events),3,'Completed files must not download twice')
            finally:
                ui.close()

if __name__=='__main__': unittest.main()
