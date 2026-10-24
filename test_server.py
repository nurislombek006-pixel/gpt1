import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import Store, filename, message_link
from server import App


class CoreTests(unittest.TestCase):
    def test_links(self):
        self.assertEqual(message_link('https://t.me/example/35?single'), ('example', 35))
        self.assertEqual(message_link('t.me/s/example/35'), ('example', 35))
        self.assertEqual(message_link('https://t.me/c/123456/7'), (-100123456, 7))
        self.assertEqual(message_link('https://t.me/c/123456/99/7'), (-100123456, 7))

    def test_invalid_links(self):
        for url in ['https://t.me.evil.test/example/1', 'https://t.me/+invite',
                    'https://t.me/example/0', 'https://t.me/c/0/1',
                    'https://t.me/example/nope', 'https://evil.test/example/3']:
            with self.assertRaises(ValueError, msg=url):
                message_link(url)

    def test_filename_cannot_escape_temp(self):
        self.assertEqual(filename('../../secret.mp4', 'x.bin'), 'secret.mp4')
        self.assertNotIn('\\', filename('C:\\test\\foo.mp4', 'x.bin'))
        self.assertTrue(filename('x'*500+'.mp4', 'x.bin').endswith('.mp4'))

    def test_search_and_durable_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'db.sqlite3'
            store = Store(path)
            store.dialog(42, 'Экономика', 'ECON_TEST')
            self.assertEqual(store.search('эКоНоМ')[0]['id'], 42)
            self.assertEqual(store.search('@econ_test')[0]['id'], 42)
            self.assertEqual(store.search('%'), [])
            job = store.enqueue({'chat': 42, 'limit': 10})
            self.assertEqual(store.next(), (job, {'chat': 42, 'limit': 10}))
            store.delivered(42, 8, 123, job)
            store.db.close()
            recovered = Store(path)
            self.assertEqual(recovered.next()[0], job)
            self.assertTrue(recovered.seen(42, 8))
            self.assertEqual(recovered.status()[0]['sent'], 1)
            recovered.db.close()

    def test_queue_limit_and_cancel(self):
        store = Store(':memory:')
        for i in range(20):
            store.enqueue({'chat': i})
        with self.assertRaises(ValueError):
            store.enqueue({'chat': 99})
        store.cancel_queued()
        self.assertIsNone(store.next())
        store.db.close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(':memory:')
        self.job_id = self.store.enqueue({'chat': 42})
        self.uploaded = []

        async def download(msg, file, progress_callback):
            file.write(b'hello')
            progress_callback(5, 5)
            return 'downloaded'

        async def upload(peer, path, **kwargs):
            self.uploaded.append(Path(path).read_bytes())
            return SimpleNamespace(id=100)

        self.app = App(SimpleNamespace(download_media=download),
                       SimpleNamespace(send_file=upload), 11, self.store,
                       self.temp.name, 1024*1024)
        self.app.recipient = 'owner-peer'
        self.msg = SimpleNamespace(id=7, photo=True, document=None,
                                   file=SimpleNamespace(size=5, name=None, ext='.jpg', mime_type='image/jpeg'),
                                   message='caption')

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_success_cleanup_and_dedup(self):
        await self.app.deliver(self.job_id, 42, self.msg)
        await self.app.deliver(self.job_id, 42, self.msg)
        self.assertEqual(self.uploaded, [b'hello'])
        self.assertTrue(self.store.seen(42, 7))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(self.store.status()[0]['skipped'], 1)

    async def test_failed_upload_cleans_and_does_not_mark_delivered(self):
        async def failed(*args, **kwargs):
            raise ValueError('Upload rejected')
        self.app.bot.send_file = failed
        with self.assertRaises(ValueError):
            await self.app.deliver(self.job_id, 42, self.msg)
        self.assertFalse(self.store.seen(42, 7))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_cancel_during_upload_cleans_file(self):
        ready = asyncio.Event()
        async def blocked(*args, **kwargs):
            ready.set()
            await asyncio.Event().wait()
        self.app.bot.send_file = blocked
        task = asyncio.create_task(self.app.deliver(self.job_id, 42, self.msg))
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.store.seen(42, 7))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_progress_enforces_actual_download_limit(self):
        self.app.max_size = 2
        self.msg.file.size = None
        with self.assertRaises(ValueError):
            await self.app.deliver(self.job_id, 42, self.msg)
        self.assertFalse(self.uploaded)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_other_users_and_groups_ignored(self):
        await self.app.on_message(SimpleNamespace(is_private=True, sender_id=99))
        await self.app.on_message(SimpleNamespace(is_private=False, sender_id=11))
        self.assertEqual(self.app.recipient, 'owner-peer')

    async def test_owner_link_queues_specific_message(self):
        async def peer():
            return 'owner-peer'
        async def say(*args, **kwargs):
            pass
        self.app.say = say
        await self.app.on_message(SimpleNamespace(is_private=True, sender_id=11,
                                                 raw_text='https://t.me/example/123', get_input_chat=peer))
        self.store.next()  # Initial setup job.
        self.assertEqual(self.store.next()[1], {'chat': 'example', 'message': 123})


if __name__ == '__main__':
    unittest.main()
