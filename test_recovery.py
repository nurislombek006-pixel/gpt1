import asyncio
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telethon import errors
from recovery import Recovery


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FakeClient:
    def __init__(self, owner=100, password=False):
        self.owner, self.password_required = owner, password
        self.session = object()
        self.connected = False
        self.approval = asyncio.Event()
        self.logged_out = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def qr_login(self):
        async def wait():
            await self.approval.wait()
            if self.password_required:
                raise errors.SessionPasswordNeededError(None)
        return SimpleNamespace(url='private-login-payload', wait=wait)

    async def sign_in(self, *, password):
        if password != 'correct-private-password':
            raise errors.PasswordHashInvalidError(None)

    async def get_me(self):
        return SimpleNamespace(id=self.owner, bot=False)

    async def log_out(self):
        self.logged_out = True


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        client = FakeClient(**kwargs)
        config = {'TG_MEDIA_PAUSED':'1', 'TG_RECOVERY_EXPIRES':'10000',
                  'TG_API_ID':'1', 'TG_API_HASH':'test-api', 'TG_OWNER_ID':'100',
                  'TG_RECOVERY_HASH':digest('browser'),
                  'TG_RECOVERY_EXPORT_HASH':digest('operator')}
        obj = Recovery(config, client_factory=lambda *a, **k:client,
                       renderer=lambda _: '<svg></svg>', clock=lambda:100)
        self.addAsyncCleanup(obj.close)
        return obj, client

    async def until(self, obj, state):
        async def wait():
            while obj.state != state:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)

    async def test_disabled_expired_and_wrong_tokens_cannot_start_or_export(self):
        obj, _ = self.make()
        for headers in ({}, {'authorization':'Bearer wrong'}):
            code, _, _ = await obj.route('POST','/reconnect/start',headers,b'{}')
            self.assertEqual(code,'404 Not Found')
        self.assertIsNone(obj.task)
        self.assertFalse(obj.authorized('Bearer browser',export=True))
        self.assertFalse(obj.authorized('Bearer operator'))
        obj.config['TG_MEDIA_PAUSED']='0'
        self.assertFalse(obj.enabled())
        obj.config['TG_MEDIA_PAUSED']='1'
        obj.expires=99
        self.assertFalse(obj.enabled())

    async def test_owner_checked_disconnect_before_export_and_separate_operator_key(self):
        obj, client = self.make()
        await obj.start()
        await self.until(obj,'qr')
        self.assertEqual(obj.status()['svg'],'<svg></svg>')
        client.approval.set()
        with patch('recovery.StringSession.save',return_value='private-session'):
            await obj.task
        self.assertEqual(obj.state,'authorized')
        self.assertFalse(client.connected)
        self.assertNotIn('private-session',json.dumps(obj.status()))
        code,_,body=await obj.route('GET','/reconnect/session',{'authorization':'Bearer browser'},b'')
        self.assertEqual(code,'404 Not Found')
        code,_,body=await obj.route('GET','/reconnect/session',{'authorization':'Bearer operator'},b'')
        self.assertEqual(json.loads(body),{'session':'private-session'})

    async def test_wrong_account_is_logged_out_and_session_not_exported(self):
        obj, client = self.make(owner=101)
        await obj.start()
        await self.until(obj,'qr')
        client.approval.set()
        await obj.task
        self.assertEqual(obj.state,'wrong_owner')
        self.assertTrue(client.logged_out)
        self.assertEqual(obj.session,'')

    async def test_two_factor_password_kept_private_and_approval_completes(self):
        obj, client = self.make(password=True)
        await obj.start()
        await self.until(obj,'qr')
        client.approval.set()
        await self.until(obj,'password')
        await obj.password('wrong')
        self.assertEqual(obj.state,'incorrect_password')
        with patch('recovery.StringSession.save',return_value='private-session'):
            await obj.password('correct-private-password')
            await obj.task
        self.assertEqual(obj.state,'authorized')
        self.assertNotIn('password', json.dumps(obj.status()))

    async def test_start_is_idempotent_and_shutdown_cancels_login(self):
        obj, client=self.make()
        await obj.start()
        task=obj.task
        await obj.start()
        self.assertIs(obj.task,task)
        await self.until(obj,'qr')
        await obj.close()
        self.assertEqual(obj.state,'stopped')
        self.assertFalse(client.connected)


if __name__=='__main__':
    unittest.main()
