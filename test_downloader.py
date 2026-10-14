import tempfile
import unittest
import threading
import sqlite3
from contextlib import closing
from pathlib import Path
from downloader import Catalog,Entry,Halt,FileFailure,ErrorPolicy,Runner,safe_name,parse_size,remote_from_url,url_for,publish_download,prepare_download_history

class FakeChrome:
    def __init__(self,bad=(),global_error=None):
        self.bad,self.global_error=set(bad),global_error
        self.clicked=[]
    def check(self): pass
    def navigate(self,parent): pass
    def find_row(self,parent,name):
        if self.global_error: raise Halt(self.global_error)
        if name in self.bad: raise FileFailure('Файл недоступен')
        return Entry(name,False,'1 КБ','stamp')
    def entry(self,row): return row
    def click_download(self,parent,name): self.clicked.append((parent,name))
    def download_file(self,parent,name):
        self.clicked.append((parent,name))
        p=self.downloads/name
        p.write_bytes(b'x'*1024)
        return p

class CoreTests(unittest.TestCase):
    def test_download_history_cleanup_preserves_other_data(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp);default=profile/'Default';default.mkdir()
            history=default/'History'
            with closing(sqlite3.connect(history)) as c:
                for table in ('downloads','downloads_url_chains','downloads_slices','visits'):
                    c.execute('CREATE TABLE '+table+' (id INTEGER)')
                    c.execute('INSERT INTO '+table+' VALUES (1)')
                c.commit()
            artifact=profile/'saved.bin';artifact.write_bytes(b'keep')
            prepare_download_history(profile)
            with closing(sqlite3.connect(history)) as c:
                for table in ('downloads','downloads_url_chains','downloads_slices'):
                    self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
                self.assertEqual(c.execute('SELECT count(*) FROM visits').fetchone()[0],1)
            with closing(sqlite3.connect(default/'History-before-copier-download-fix.sqlite3')) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM downloads').fetchone()[0],1)
            self.assertEqual(artifact.read_bytes(),b'keep')

    def test_url_cyrillic(self):
        for remote in ['', 'бизнес/копии от 29.09', 'a+b/#test']:
            self.assertEqual(remote_from_url(url_for(remote)),remote)
        with self.assertRaises(Halt): remote_from_url('https://evil.example/client/disk')
        with self.assertRaises(Halt): remote_from_url('https://disk.yandex.ru/client/disk-other')

    def test_windows_names(self):
        for name in ['../x','CON','abc.','a:b','a?b','.' ,'x'*300]:
            result=safe_name(name)
            self.assertNotIn('/',result)
            self.assertNotIn(':',result)
            self.assertLessEqual(len(result),180)
        self.assertNotEqual(safe_name('a:b'),safe_name('a?b'))
        self.assertEqual(safe_name('Фотографии.jpg'),'Фотографии.jpg')

    def test_rounded_size(self):
        lo,hi=parse_size('12,5\xa0КБ')
        self.assertLess(lo,12500)
        self.assertGreater(hi,12800)
        self.assertIsNone(parse_size(''))
        self.assertEqual(parse_size('0 Б')[0],0)

    def test_catalog_tree_case_collision_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            c=Catalog(temp,'бизнес')
            c.record('бизнес',[Entry('Пустая',True,'',''),Entry('A.txt',False,'1 КБ','stamp'),Entry('a.txt',False,'1 КБ','stamp')])
            self.assertTrue((Path(temp)/'Пустая').is_dir())
            self.assertNotEqual(str(c.path('бизнес/A.txt')).lower(),str(c.path('бизнес/a.txt')).lower())
            p=c.path('бизнес/A.txt'); p.write_bytes(b'x'*1024)
            c.done('бизнес/A.txt',p)
            c.close()
            c=Catalog(temp,'бизнес'); c.verify_completed()
            self.assertEqual(c.stats()[1],1)
            p.write_bytes(b'corrupt')
            c.verify_completed()
            self.assertEqual(c.stats()[1],0)
            c.close()
            with self.assertRaises(Halt): Catalog(temp,'фото')

    def test_changed_remote_requeues(self):
        with tempfile.TemporaryDirectory() as temp:
            c=Catalog(temp,'')
            c.record('',[Entry('one',False,'1 КБ','old')])
            p=c.path('one'); p.write_bytes(b'x'*1024); c.done('one',p)
            c.record('',[Entry('one',False,'2 КБ','new')])
            self.assertEqual(c.stats()[1],0)
            c.close()

    def test_publish_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            src=p/'downloads'/'file.txt'; src.parent.mkdir(); src.write_bytes(b'new')
            dest=p/'mirror'/'file.txt'; dest.parent.mkdir(); dest.write_bytes(b'old')
            result=publish_download(src,dest)
            self.assertEqual(dest.read_bytes(),b'old')
            self.assertEqual(result.read_bytes(),b'new')
            self.assertFalse(src.exists())

    def test_failed_file_skipped_and_resumed_in_original_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'copy'; downloads=Path(temp)/'downloads'; downloads.mkdir()
            c=Catalog(target,'')
            c.record('',[Entry('nested',True,'','')])
            c.record('nested',[Entry('bad.txt',False,'1 КБ','stamp'),Entry('good.txt',False,'1 КБ','stamp')])
            runner=Runner('',target,downloads,threading.Event(),lambda *v:None,ErrorPolicy(True))
            ui=FakeChrome(bad=['bad.txt'])
            ui.downloads=downloads
            runner.process_files(ui,c)
            self.assertEqual(ui.clicked,[('nested','good.txt')])
            self.assertEqual(c.failure_count(),1)
            self.assertEqual(c.stats()[1],1)
            report=(target/'Не скачано.csv').read_text(encoding='utf-8-sig')
            self.assertIn('nested/bad.txt',report)
            self.assertIn(str(Path('nested')/'bad.txt'),report)
            c.close()
            c=Catalog(target,'')
            ui=FakeChrome()
            ui.downloads=downloads
            runner.process_files(ui,c)
            self.assertEqual(ui.clicked,[('nested','bad.txt')])
            self.assertEqual((target/'nested'/'bad.txt').stat().st_size,1024)
            self.assertEqual(c.stats()[1],2)
            self.assertEqual(c.failure_count(),0)
            self.assertNotIn('bad.txt',(target/'Не скачано.csv').read_text(encoding='utf-8-sig'))
            c.close()

    def test_remember_answer_and_bound_retries(self):
        calls=[]
        def ask(*args): calls.append(args); return 'retry',True
        policy=ErrorPolicy(False,ask)
        self.assertEqual(policy.decide('first','error',1),'retry')
        self.assertEqual(policy.decide('second','error',1),'retry')
        self.assertEqual(policy.decide('second','error',3),'skip')
        self.assertEqual(len(calls),1)

    def test_do_not_remember_answer(self):
        calls=[]
        def ask(*args): calls.append(args); return 'skip',False
        policy=ErrorPolicy(False,ask)
        for remote in ['first','second']: self.assertEqual(policy.decide(remote,'error',1),'skip')
        self.assertEqual(len(calls),2)

    def test_global_stop_is_never_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'copy'; downloads=Path(temp)/'downloads'; downloads.mkdir()
            c=Catalog(target,'')
            c.record('',[Entry('one.txt',False,'1 КБ','stamp'),Entry('two.txt',False,'1 КБ','stamp')])
            runner=Runner('',target,downloads,threading.Event(),lambda *v:None,ErrorPolicy(True))
            ui=FakeChrome(global_error='Вкладка закрыта')
            with self.assertRaises(Halt): runner.process_files(ui,c)
            self.assertEqual(ui.clicked,[])
            self.assertEqual(c.failure_count(),1)
            self.assertEqual(c.stats()[1],0)
            c.close()

    def test_old_catalog_without_error_table_is_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            c=Catalog(temp,'')
            c.record('',[Entry('file',False,'1 КБ','stamp')])
            c.db.execute('DROP TABLE failures'); c.db.commit(); c.close()
            c=Catalog(temp,'')
            c.failed('file','Test')
            self.assertEqual(c.failure_count(),1)
            c.close()

    def test_remembered_retry_exhausts_then_moves_to_next_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'copy'; downloads=Path(temp)/'downloads'; downloads.mkdir()
            c=Catalog(target,'')
            c.record('',[Entry('bad.txt',False,'1 КБ','stamp'),Entry('good.txt',False,'1 КБ','stamp')])
            calls=[]
            def ask(*args): calls.append(args); return 'retry',True
            runner=Runner('',target,downloads,threading.Event(),lambda *v:None,ErrorPolicy(False,ask))
            ui=FakeChrome(bad=['bad.txt'])
            ui.downloads=downloads
            runner.process_files(ui,c)
            self.assertEqual(ui.clicked,[('','good.txt')])
            self.assertEqual(c.db.execute('SELECT count FROM failures WHERE remote=?',('bad.txt',)).fetchone()[0],3)
            self.assertEqual(len(calls),1)
            c.close()

    def test_manual_stop_preserves_remaining_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'copy'; downloads=Path(temp)/'downloads'; downloads.mkdir()
            c=Catalog(target,'')
            c.record('',[Entry('bad',False,'1 КБ','stamp'),Entry('good',False,'1 КБ','stamp')])
            runner=Runner('',target,downloads,threading.Event(),lambda *v:None,ErrorPolicy(False,lambda *v:('stop',False)))
            with self.assertRaises(Halt): runner.process_files(FakeChrome(bad=['bad']),c)
            self.assertEqual(c.db.execute('SELECT state FROM files WHERE remote=?',('good',)).fetchone()[0],'pending')
            self.assertEqual(c.failure_count(),1)
            c.close()

    def test_metadata_name_does_not_overwrite_downloaded_file(self):
        with tempfile.TemporaryDirectory() as temp:
            c=Catalog(temp,'')
            c.record('',[Entry('Не скачано.csv',False,'1 КБ','stamp'),Entry('.yd-failures.csv.tmp',False,'1 КБ','stamp')])
            for remote in ['Не скачано.csv','.yd-failures.csv.tmp']:
                p=c.path(remote); p.write_bytes(b'original')
                c.done(remote,p)
                self.assertEqual(p.read_bytes(),b'original')
                self.assertNotEqual(p,Path(temp)/remote)
            c.close()

if __name__=='__main__': unittest.main()
