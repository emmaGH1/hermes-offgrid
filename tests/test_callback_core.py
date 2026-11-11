"""Offline callback and outbound policy tests."""
import asyncio
import importlib.util
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ussd_test_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from ussd_test_plugin import adapter

class Store:
    def __init__(self):
        self.seen = set()
    def get_last_answer(self, phone):
        return 'last reply'
    def admit_question(self, session, phone, now, *, limit=3):
        key = (session, phone)
        if key in self.seen:
            return 'duplicate'
        if len(self.seen) >= limit:
            return 'rate_limited'
        self.seen.add(key)
        return 'accepted'

class CallbackTests(unittest.IsolatedAsyncioTestCase):
 async def test_callback_auth_admission_and_retries(self):
     obj = object.__new__(adapter.USSDAdapter)
     obj.config = SimpleNamespace(extra={'ussd_callback_secret': 'secret', 'ussd_allowed_users': '+12345678901'})
     obj._store = Store()
     obj._queue = asyncio.Queue(maxsize=100)
     app = web.Application(client_max_size=4096)
     app.router.add_post('/ussd', obj.callback)
     async with TestClient(TestServer(app)) as client:
         async def post(text='', token='secret', phone='+12345678901', session='s1'):
             resp = await client.post('/ussd', params={'token': token}, data={'sessionId': session, 'phoneNumber': phone, 'text': text})
             assert resp.status == 200
             return await resp.text()
         with patch.object(adapter, 'get_scoped_secret', side_effect=lambda n: '+12345678901' if n == 'USSD_ALLOWED_USERS' else None):
             assert (await post()).startswith('CON hermes')
             assert (await post('1')).startswith('CON ')
             assert await post('2') == 'END last reply'
             assert (await post('1*hello', token='bad')).startswith('END request')
             assert (await post('1*hello', phone='+12345678902')).startswith('END request')
             assert (await post('1*hello')).startswith('END got it')
             assert (await post('1*hello')).startswith('END got it')
             assert obj._queue.qsize() == 1
             await post('1*hello', session='s2')
             await post('1*hello', session='s3')
             assert (await post('1*hello', session='s4')).startswith('END too many')
             assert obj._queue.qsize() == 3

class SendTests(unittest.IsolatedAsyncioTestCase):
 async def test_standalone_rejects_media(self):
     result = await adapter.standalone_send(None, '+12345678901', 'hello', media_files=['x'])
     assert result['success'] is False

 async def test_delivery_success_failure_and_policy(self):
     class Store:
         answer = None
         def set_last_answer(self, phone, answer): self.answer = answer
     class Client:
         def __init__(self, success): self.success = success
         async def send_sms(self, phone, text):
             return SimpleNamespace(success=self.success, message_ids=('first',))
     store = Store()
     with patch.object(adapter, 'get_scoped_secret', return_value='+12345678901'):
         self.assertFalse((await adapter._deliver(None, Client(True), store, '+12345678902', 'x')).success)
         self.assertFalse((await adapter._deliver(None, Client(False), store, '+12345678901', 'x')).success)
         self.assertIsNone(store.answer)
         self.assertTrue((await adapter._deliver(None, Client(True), store, '+12345678901', 'full')).success)
         self.assertEqual(store.answer, 'full')

 async def test_sandbox_fails_safe(self):
     with patch.object(adapter, 'get_scoped_secret', return_value=None):
         for value in ('typo', ' True ', 'on'):
             config = SimpleNamespace(extra={'at_api_key':'x', 'at_username':'sandbox', 'at_sandbox':value})
             self.assertTrue(adapter._client(None, config)._sandbox)
         config.extra['at_sandbox'] = ' false '
         self.assertFalse(adapter._client(None, config)._sandbox)

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import sqlite3
        from gateway.config import PlatformConfig
        self.values = {'AT_API_KEY': 'dummy', 'AT_USERNAME': 'sandbox',
                       'USSD_CALLBACK_SECRET': 's' * 32,
                       'USSD_ALLOWED_USERS': '+12345678901', 'USSD_PORT': '0'}
        self.secrets = patch.object(adapter, 'get_scoped_secret', side_effect=self.values.get)
        self.secrets.start()
        self.addCleanup(self.secrets.stop)
        self.db = patch.object(adapter, 'plugin_db', side_effect=lambda _: sqlite3.connect(':memory:'))
        self.db.start()
        self.addCleanup(self.db.stop)
        with patch("gateway.platform_registry.platform_registry.is_registered", return_value=True):
            self.obj = adapter.USSDAdapter(PlatformConfig())

    async def asyncTearDown(self):
        await self.obj.disconnect()

    async def test_listener_dispatch_and_shutdown_drain(self):
        from unittest.mock import AsyncMock
        self.obj.handle_message = AsyncMock()
        self.assertTrue(await self.obj.connect())
        session = self.obj._session
        worker = self.obj._worker
        self.assertTrue(await self.obj.connect())
        self.assertIs(session, self.obj._session)
        port = self.obj._runner.addresses[0][1]
        async with session.post(f'http://127.0.0.1:{port}/ussd',
                                params={'token': 's' * 32},
                                data={'sessionId': 'lifecycle', 'phoneNumber': '+12345678901',
                                      'text': '1*hello'}) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.text()).startswith('END got it'))
        await self.obj.disconnect()
        self.obj.handle_message.assert_awaited_once()
        event = self.obj.handle_message.call_args.args[0]
        self.assertEqual(event.text, 'hello')
        self.assertFalse(event.allow_gateway_control)
        self.assertTrue(session.closed)
        self.assertTrue(worker.done())
        self.assertIsNone(self.obj._store)

    async def test_invalid_config_and_bind_failure_cleanup(self):
        self.values['USSD_CALLBACK_SECRET'] = 'short'
        self.assertFalse(await self.obj.connect())
        self.values['USSD_CALLBACK_SECRET'] = 's' * 32
        from unittest.mock import AsyncMock
        with patch.object(adapter.web.TCPSite, 'start', new=AsyncMock(side_effect=OSError('private detail'))):
            with self.assertLogs(adapter.logger, level='WARNING') as logs:
                self.assertFalse(await self.obj.connect())
        self.assertNotIn('private detail', ''.join(logs.output))
        self.assertIsNone(self.obj._runner)
        self.assertIsNone(self.obj._session)
        self.assertIsNone(self.obj._store)

    async def test_busy_http_does_not_admit_question(self):
        self.obj._store = Store()
        self.obj._queue = asyncio.Queue(maxsize=1)
        self.obj._queue.put_nowait(('existing', '+12345678901', 'q'))
        app = web.Application()
        app.router.add_post('/ussd', self.obj.callback)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/ussd', params={'token': 's' * 32},
                                         data={'sessionId': 'busy', 'phoneNumber': '+12345678901', 'text': '1*hello'})
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), 'END busy. please try again.')
            self.assertEqual(self.obj._store.seen, set())
        self.obj._store = None

    async def test_adapter_send_and_standalone_success(self):
        from unittest.mock import AsyncMock
        self.assertFalse((await self.obj.send('+12345678901', 'hello')).success)
        self.assertTrue(await self.obj.connect())
        client = SimpleNamespace(send_sms=AsyncMock(return_value=SimpleNamespace(success=True, message_ids=('sms1',))))
        with patch.object(adapter, '_client', return_value=client):
            result = await self.obj.send('+12345678901', 'hello')
            self.assertTrue(result.success)
            self.assertEqual(self.obj._store.get_last_answer('+12345678901'), 'hello')
            result = await adapter.standalone_send(self.obj.config, '+12345678901', 'scheduled')
            self.assertEqual(result, {'success': True, 'message_id': 'sms1'})
        self.assertEqual(client.send_sms.await_count, 2)

class DependencyTests(unittest.TestCase):
    def test_registration_without_aiohttp(self):
        import builtins
        from unittest.mock import Mock
        real_import = builtins.__import__
        def without_aiohttp(name, *args, **kwargs):
            if name == 'aiohttp':
                raise ModuleNotFoundError("No module named 'aiohttp'", name='aiohttp')
            return real_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('ussd_test_plugin.no_dependency_adapter', ROOT / 'adapter.py')
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=without_aiohttp):
            spec.loader.exec_module(module)
        ctx = Mock()
        module.register(ctx)
        self.assertIsNone(module.aiohttp)
        with patch.object(module.importlib.util, 'find_spec', return_value=None):
            self.assertFalse(ctx.register_platform.call_args.kwargs['check_fn']())

class SMSCallbackTests(unittest.IsolatedAsyncioTestCase):
 async def test_auth_dedupe_rate_bounds_and_busy(self):
     import sqlite3
     obj = object.__new__(adapter.USSDAdapter)
     obj.config = SimpleNamespace(extra={'ussd_callback_secret': 'secret'})
     obj._store = adapter.StateStore(sqlite3.connect(':memory:'))
     obj._queue = asyncio.Queue(maxsize=4)
     app = web.Application(client_max_size=4096)
     app.router.add_post('/sms', obj.sms_callback)
     app.router.add_post('/ussd', obj.callback)
     async with TestClient(TestServer(app)) as client:
         async def sms(**changes):
             data = {'id':'same', 'from':'+12345678901', 'to':'12345', 'text':'hello'}
             data.update(changes)
             response = await client.post('/sms?token=secret', data=data)
             self.assertEqual(response.status, 200)
             self.assertEqual(await response.text(), '')
         with patch.object(adapter, 'get_scoped_secret', side_effect=lambda n: '+12345678901' if n == 'USSD_ALLOWED_USERS' else None):
             await sms(**{'from':'+12345678902'})
             await sms(text='x'*501)
             await client.post('/sms?token=wrong', data={'id':'bad','from':'+12345678901','to':'1','text':'x'})
             self.assertEqual(obj._queue.qsize(), 0)
             await sms(); await sms()
             await client.post('/ussd?token=secret', data={'sessionId':'same','phoneNumber':'+12345678901','text':'1*hi'})
             await sms(id='second'); await sms(id='third')
             self.assertEqual(obj._queue.qsize(), 3)
             self.assertEqual([obj._queue.get_nowait()[0] for _ in range(3)], ['sms:same','ussd:same','sms:second'])
             obj._queue = asyncio.Queue(maxsize=1); obj._queue.put_nowait(('x','x','x'))
             await sms(id='busy')
             self.assertEqual(obj._queue.qsize(), 1)
     obj._store.close()

 async def test_sender_configured(self):
     with patch.object(adapter, 'get_scoped_secret', return_value=None):
         client = adapter._client(None, SimpleNamespace(extra={'at_api_key':'x','at_username':'sandbox','at_sender_id':'12345'}))
         self.assertEqual(client._sender_id, '12345')


class SMSUnicodeTests(unittest.IsolatedAsyncioTestCase):
    setUp = LifecycleTests.setUp
    asyncTearDown = LifecycleTests.asyncTearDown
    async def test_unicode_limit_and_token_warning_throttle(self):
        from unittest.mock import AsyncMock
        self.obj.handle_message = AsyncMock()
        self.assertTrue(await self.obj.connect())
        port = self.obj._runner.addresses[0][1]
        url = f'http://127.0.0.1:{port}/sms'
        with self.assertLogs(adapter.logger, level='WARNING') as logs:
            for _ in range(3):
                async with self.obj._session.post(url + '?token=wrong', data={}) as r:
                    self.assertEqual(r.status, 200)
        self.assertEqual(logs.output, ['WARNING:ussd_test_plugin.adapter:SMS callback rejected: token'])
        for idx, text in enumerate(('你' * 500, '😀' * 500, 'x' * 501)):
            async with self.obj._session.post(url, params={'token': 's' * 32},
                    data={'id': f'unicode-{idx}', 'from': '+12345678901', 'to': '12345', 'text': text}) as r:
                self.assertEqual(r.status, 200)
        await self.obj.disconnect()
        self.assertEqual(self.obj.handle_message.await_count, 2)
        self.assertEqual([c.args[0].text for c in self.obj.handle_message.call_args_list], ['你' * 500, '😀' * 500])


class RateConfigurationTests(unittest.TestCase):
    def test_rate_limit_is_bounded_and_fails_safe(self):
        with patch.object(adapter, 'get_scoped_secret', return_value=None):
            for setting, expected in [('10', 10), ('0', 1), ('1000', 30), ('bad', 3), (None, 3)]:
                self.assertEqual(adapter._question_limit(SimpleNamespace(extra={'offgrid_rate_limit': setting})), expected)

class SMSRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_ten_message_budget_and_duplicate(self):
        import sqlite3
        obj = object.__new__(adapter.USSDAdapter)
        obj.config = SimpleNamespace(extra={'ussd_callback_secret': 's' * 32, 'offgrid_rate_limit': '10'})
        obj._store = adapter.StateStore(sqlite3.connect(':memory:'))
        self.addCleanup(obj._store.close)
        obj._queue = asyncio.Queue(maxsize=100)
        app = web.Application()
        app.router.add_post('/sms', obj.sms_callback)
        with patch.object(adapter, 'get_scoped_secret', side_effect=lambda n: '+12345678901' if n == 'USSD_ALLOWED_USERS' else None):
            async with TestClient(TestServer(app)) as client:
                for i in [0, 0, *range(1, 11)]:
                    response = await client.post('/sms', params={'token': 's' * 32},
                        data={'id': str(i), 'from': '+12345678901', 'to': '12345', 'text': 'hello'})
                    self.assertEqual(response.status, 200)
                self.assertEqual(obj._queue.qsize(), 10)
