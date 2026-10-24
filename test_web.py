import json
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from web import page, status

class WebTests(unittest.TestCase):
    def test_status_does_not_promise_telegram_ready(self):
        self.assertEqual(status(None, False), 'needs_configuration')
        self.assertEqual(status(None, True), 'starting_or_running')
        self.assertNotIn(b'TG_BOT_TOKEN', page('needs_configuration'))

    def test_real_http_without_secrets(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k:v for k,v in os.environ.items() if not k.startswith('TG_')}
        env['PORT'] = str(port)
        proc = subprocess.Popen([sys.executable, '-u', 'web.py'], cwd=Path(__file__).parent,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('Free web service ready', proc.stdout.readline())
            base = f'http://127.0.0.1:{port}'
            with urlopen(base+'/healthz', timeout=3) as response:
                self.assertEqual(json.load(response), {'http':'ok','telegram':'needs_configuration'})
            with urlopen(base, timeout=3) as response:
                body = response.read().decode()
                self.assertIn('https://t.me/frodgrambot', body)
                self.assertNotIn('TG_USER_SESSION', body)
            with urlopen(Request(base, method='HEAD'), timeout=3) as response:
                self.assertEqual(response.read(), b'')
            for request, expected in [(base+'/anything',404), (Request(base,method='POST',data=b'x'),405)]:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code,expected)
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

if __name__ == '__main__':
    unittest.main()
