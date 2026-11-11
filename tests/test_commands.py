"""Offline tests for the SMS/USSD gateway command allowlist and keywords."""
import asyncio
import importlib.util
import pathlib
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if 'ussd_test_plugin' not in sys.modules:
    spec = importlib.util.spec_from_file_location('ussd_test_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
from ussd_test_plugin import adapter
from ussd_test_plugin.at_client import _gsm_cost, clean_sms, sms_segments

route = adapter.route_text
BLOCKED = ('reply', adapter.BLOCKED_TEXT)
HELP = ('reply', adapter.HELP_TEXT)


class RouteTests(unittest.TestCase):
    def test_allowed_commands_get_control(self):
        for text, expected in [('/new', '/new'), ('/sessions', '/sessions'), ('/resume', '/resume'),
                               ('/resume 2', '/resume 2'),
                               ('/model', '/model'), ('/model gpt-5', '/model --session gpt-5'),
                               ('/model reset', '/model reset'), ('/status', '/status'), ('/retry', '/retry'),
                               ('/stop', '/stop'), ('/title', '/title'), ('/reset', '/new')]:
            self.assertEqual(route(text), ('command', expected), text)

    def test_blocked_and_unknown_commands(self):
        for text in ['/yolo', '/approve', '/deny', '/approvals', '/config', '/update', '/tools', '/pause',
                     '/sethome', '/set-home', '/import', '/export', '/snapshot', '/snap', '/rollback', '/bg',
                     '/goal', '/loop', '/proactive', '/heartbeat', '/hb', '/personality', '/restart',
                     '/always', '/confirm', '/cancel', '/history', '/q', '/steer', '/s', '/upgrade', '/v',
                     '/my-plugin-cmd', '/some_skill do it', '/', '/ ', '/usr/bin/ls', '/new/../x', '/whoami',
                     '/model gpt-5 --global', '/model --GLOBAL x']:
            self.assertEqual(route(text), BLOCKED, text)

    def test_parser_bypasses_are_classified(self):
        for text in ['  /yolo', '\n/yolo', '/YOLO', '/yolo@bot', '/Yolo@hermes_bot on', '@image:/tmp/a.png\n/yolo',
                     '@FILE:x y\n@url:http://z\n  /yolo', '／yolo', '∕yolo', '⁄yolo', '⧸yolo']:
            self.assertEqual(route(text), BLOCKED, repr(text))
        for text in ['  /NEW', '/New@bot', '/reset', '／new', '@image:/tmp/a.png\n/new', '/new\n']:
            self.assertEqual(route(text), ('command', '/new'), repr(text))
        # Text that Hermes would not parse as a command stays plain chat (no control).
        for text in ['hello\n/yolo', 'please run /yolo', 'x/new']:
            self.assertEqual(route(text), ('chat', text), repr(text))

    def test_every_hermes_slash_parse_is_classified(self):
        from gateway.platforms.event import MessageEvent
        samples = ['/yolo', ' /x', '@image:a\n/approve', '\t/new', '/', '/a/b', 'hi', '@url:x\nhi', '/help',
                   '/model x', '@image:a\n@file:b\n\n /deny now']
        for text in samples:
            event = MessageEvent(text=text)
            if event.is_command():
                self.assertNotEqual(route(text)[0], 'chat', repr(text))
            kind, out = route(text)
            if kind == 'command':
                checked = MessageEvent(text=out)
                self.assertIn(checked.get_command(), adapter._COMMANDS | {'confirm', 'cancel'})

    def test_keywords(self):
        for text, expected in [('NEW', '/new'), ('new', '/new'), (' New ', '/new'), ('SESSIONS', '/sessions'),
                               ('sessions', '/sessions'), ('STATUS', '/status'), ('Resume', '/resume'),
                               ('RESUME 3', '/resume 3'), ('resume 12', '/resume 12'), ('MODEL', '/model'),
                               ('model gpt-5.5', '/model --session gpt-5.5'),
                               ('MODEL openai/gpt-5', '/model --session openai/gpt-5'),
                               ('model reset', '/model reset')]:
            self.assertEqual(route(text), ('command', expected), text)
        self.assertEqual(route('HELP'), HELP)
        self.assertEqual(route('help'), HELP)
        self.assertEqual(route('/help'), HELP)
        self.assertEqual(route('/help@bot'), HELP)

    def test_sentences_starting_with_keywords_stay_chat(self):
        for text in ['new ideas for dinner?', 'NEW.', 'sessions of parliament', 'status of my order',
                     'help me write a poem', 'resume writing tips', 'resume my CV', 'model airplanes',
                     'model 3?', 'model of the atom', 'yes', 'no', 'cancel', 'approve', 'newer', 'statuses']:
            self.assertEqual(route(text), ('chat', text), text)

    def test_confirm_window(self):
        for word in ['YES', 'yes', 'ok', 'Confirm', '/approve', '/confirm', 'approve']:
            self.assertEqual(route(word, True), ('command', '/confirm'), word)
        for word in ['NO', 'cancel', '/cancel', 'n']:
            self.assertEqual(route(word, True), ('command', '/cancel'), word)
        self.assertEqual(route('/always', True), BLOCKED)
        self.assertEqual(route('always', True), ('chat', 'always'))
        self.assertEqual(route('yes please tell me more', True), ('chat', 'yes please tell me more'))
        self.assertEqual(route('/approve'), BLOCKED)

    def test_adapter_texts_are_short_gsm(self):
        for text in (adapter.HELP_TEXT, adapter.BLOCKED_TEXT):
            self.assertEqual(clean_sms(text), text)
            self.assertLess(len(text), 300)
            self.assertLessEqual(sms_segments(text), 2)
            self.assertTrue(all(_gsm_cost(ch) for ch in text))  # GSM-7 only
        for word in ('NEW', 'SESSIONS', 'RESUME', 'MODEL', 'STATUS', 'HELP', 'YES'):
            self.assertIn(word, adapter.HELP_TEXT)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from gateway.config import PlatformConfig
        values = {'AT_API_KEY': 'dummy', 'AT_USERNAME': 'sandbox', 'OFFGRID_CALLBACK_SECRET': 's' * 32,
                  'OFFGRID_ALLOWED_USERS': '+12345678901', 'OFFGRID_PORT': '0'}
        for target in (patch.object(adapter, 'get_scoped_secret', side_effect=values.get),
                       patch.object(adapter, 'plugin_db', side_effect=lambda _: sqlite3.connect(':memory:'))):
            target.start()
            self.addCleanup(target.stop)
        with patch("gateway.platform_registry.platform_registry.is_registered", return_value=True):
            self.obj = adapter.USSDAdapter(PlatformConfig())
        self.sent = []
        client = SimpleNamespace(send_sms=AsyncMock(side_effect=lambda phone, text: self.sent.append((phone, text)) or
                                                    SimpleNamespace(success=True, message_ids=('m',))))
        p = patch.object(adapter, '_client', return_value=client)
        p.start()
        self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.obj.disconnect()

    async def sms(self, port, ident, text):
        async with self.obj._session.post(f'http://127.0.0.1:{port}/sms', params={'token': 's' * 32},
                data={'id': ident, 'from': '+12345678901', 'to': '12345', 'text': text}) as r:
            self.assertEqual(r.status, 200)

    async def drain(self):
        await asyncio.wait_for(self.obj._queue.join(), timeout=2)

    async def test_commands_controls_blocks_and_rate_limit(self):
        self.obj.handle_message = AsyncMock()
        self.assertTrue(await self.obj.connect())
        port = self.obj._runner.addresses[0][1]
        with patch.object(adapter.logger, 'info') as info:
            await self.sms(port, '1', 'NEW')
            await self.sms(port, '2', 'yes')
            await self.sms(port, '3', '/yolo')
            await self.drain()
        events = [c.args[0] for c in self.obj.handle_message.call_args_list]
        self.assertEqual([(e.text, e.allow_gateway_control) for e in events], [('/new', True), ('/confirm', True)])
        self.assertEqual(self.sent, [('+12345678901', adapter.BLOCKED_TEXT)])
        logged = ' '.join(str(a) for c in info.call_args_list for a in c.args)
        self.assertNotIn('+12345678901', logged)
        self.assertNotIn('yolo', logged)
        # The rate limit (3/min by default) still applies to commands and adapter replies.
        await self.sms(port, '4', 'HELP')
        await self.sms(port, '5', '/status')
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.obj.handle_message.await_count, 2)

    async def test_chat_help_and_expired_confirm(self):
        self.obj.handle_message = AsyncMock()
        self.assertTrue(await self.obj.connect())
        port = self.obj._runner.addresses[0][1]
        await self.sms(port, '1', 'new ideas for dinner?')
        await self.sms(port, '2', 'HELP')
        await self.sms(port, '3', 'yes')
        await self.drain()
        events = [c.args[0] for c in self.obj.handle_message.call_args_list]
        self.assertEqual([(e.text, e.allow_gateway_control) for e in events],
                         [('new ideas for dinner?', False), ('yes', False)])
        self.assertEqual(self.sent, [('+12345678901', adapter.HELP_TEXT)])
        self.assertEqual(self.obj._store.get_last_answer('+12345678901'), adapter.HELP_TEXT)

    async def test_confirm_window_cleared_by_other_message(self):
        self.obj.handle_message = AsyncMock()
        self.obj._store = adapter.StateStore(sqlite3.connect(':memory:'))
        self.obj._queue.put_nowait(('sms:a', '+12345678901', '/new'))
        self.obj._queue.put_nowait(('sms:b', '+12345678901', 'hello'))
        self.obj._queue.put_nowait(('sms:c', '+12345678901', 'yes'))
        self.obj._worker = asyncio.create_task(self.obj._dispatch())
        await self.drain()
        events = [c.args[0] for c in self.obj.handle_message.call_args_list]
        self.assertEqual([(e.text, e.allow_gateway_control) for e in events],
                         [('/new', True), ('hello', False), ('yes', False)])


if __name__ == '__main__':
    unittest.main()


class ArgumentPolicyTests(unittest.TestCase):
    """Review follow-up: a spoofable sender gets only the narrowest argument forms."""

    def test_narrow_arguments_are_refused(self):
        for text in ['/resume My Trip', '/resume --all', '/resume 2 --cross-room', '/resume \u00b2',
                     '/sessions all', '/sessions search tax', '/new Taxes', '/reset Taxes',
                     '/title Taxes', '/model x --global', '/model x --GLOBAL --provider']:
            self.assertEqual(adapter.route_text(text), ('reply', adapter.BLOCKED_TEXT), text)

    def test_session_flag_cannot_be_swallowed_by_provider(self):
        kind, command = adapter.route_text('/model gpt-5 --provider')
        self.assertEqual(kind, 'command')
        self.assertTrue(command.startswith('/model --session '), command)

    def test_superscript_resume_keyword_is_not_a_command(self):
        self.assertEqual(adapter.route_text('RESUME \u00b2'), ('chat', 'RESUME \u00b2'))
