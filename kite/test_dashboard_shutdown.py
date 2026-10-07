"""Shutdown checks with no live credentials or broker connections."""
import asyncio
import threading
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dashboard
from scanner import RealtimeScanner


class TestScannerStop(unittest.TestCase):
    def scanner(self, ticker=None):
        scanner = RealtimeScanner.__new__(RealtimeScanner)
        scanner.kws = ticker
        scanner._stop_requested = threading.Event()
        scanner._ticker_closed = threading.Event()
        return scanner

    def test_stop_before_initialization_prevents_startup(self):
        scanner = self.scanner()
        self.assertTrue(scanner.request_stop().is_set())
        scanner.resolve_tokens = Mock()
        scanner.run(threaded=True)
        scanner.resolve_tokens.assert_not_called()

    def test_connected_ticker_closes_on_reactor_without_abort(self):
        ticker = Mock()
        ticker.is_connected.return_value = True
        reactor = Mock(running=True)
        scanner = self.scanner(ticker)
        with patch('twisted.internet.reactor', reactor):
            closed = scanner.request_stop()
            ticker.close.assert_not_called()
            reactor.callFromThread.call_args.args[0]()
        ticker.close.assert_called_once_with(code=1000, reason='Application shutdown')
        ticker.factory.connector.disconnect.assert_not_called()
        self.assertFalse(closed.is_set())  # wait for the close callback, not just sendClose


class TestUvicornShutdown(unittest.TestCase):
    def test_one_sigint_drains_request_and_websocket(self):
        # A real server and real SIGINT, but no connections to Kite or other services.
        script = '''
import threading, time
from types import SimpleNamespace
import dashboard, uvicorn
dashboard.scanner.run = lambda **kwargs: None
dashboard.scanner.finalize_session = lambda: None
closed = threading.Event(); closed.set()
dashboard.scanner.request_stop = lambda: closed
dashboard.scanner.token_to_symbol = {1: 'NSE:TEST'}
dashboard.scanner.symbol_data = {1: SimpleNamespace(symbol='NSE:TEST')}
dashboard.scanner.latest_results = {1: {}}
def snapshot(*args, **kwargs):
    print('SNAPSHOT STARTED', flush=True)
    time.sleep(.2)
    return {'candles': []}
dashboard.chart_snapshot = snapshot
uvicorn.run(dashboard.app, host='127.0.0.1', port=0, log_level='info')
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', script],
                                   cwd=os.path.dirname(__file__), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        logs = []
        sockets = []
        def wait_line(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([process.stdout], [], [], .1)[0]:
                    line = process.stdout.readline()
                    logs.append(line)
                    if predicate(line):
                        return line
                if process.poll() is not None:
                    break
            self.fail('Server did not reach expected state: ' + ''.join(logs))
        try:
            line = wait_line(lambda line: 'Uvicorn running on' in line)
            port = int(re.search(r'127\.0\.0\.1:(\d+)', line).group(1))
            ws = socket.create_connection(('127.0.0.1', port), timeout=3)
            sockets.append(ws)
            ws.sendall(b'GET /ws/1 HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self.assertIn(b'101 Switching Protocols', ws.recv(4096))
            request = socket.create_connection(('127.0.0.1', port), timeout=3)
            sockets.append(request)
            request.sendall(b'GET /api/snapshot/1 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
            wait_line(lambda line: 'SNAPSHOT STARTED' in line)
            # Keep a client connected through SIGINT and acknowledge the close frame.
            def finish_websocket():
                try:
                    while True:
                        frame = ws.recv(4096)
                        if not frame:
                            break
                        if b'\x88' in frame:
                            ws.sendall(b'\x88\x82\x00\x00\x00\x00\x03\xe9')
                            break
                finally:
                    ws.close()
            receiver = threading.Thread(target=finish_websocket, daemon=True)
            receiver.start()
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=5)
            receiver.join(timeout=1)
            logs.append(output)
            self.assertEqual(process.returncode, 0, ''.join(logs))
            self.assertIn('Application shutdown complete', ''.join(logs))
            for error in ('CancelledError', 'KeyboardInterrupt', 'Exception in ASGI application'):
                self.assertNotIn(error, ''.join(logs))
        finally:
            for client in sockets:
                client.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


class TestDashboardShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_cancels_pending_broadcasts_and_unsubscribes(self):
        bridge = dashboard.LiveBridge()
        bridge.loop = asyncio.get_running_loop()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def blocked():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        bridge._schedule(blocked())
        await asyncio.wait_for(started.wait(), 1)
        socket = Mock()
        async def close(**kwargs):
            self.assertEqual(kwargs['code'], 1001)
        socket.close = close
        bridge.clients[socket] = {'symbol': 'NSE:TEST', 'timeframe': '5m'}
        bridge.add_subscription('NSE:TEST')
        await bridge.shutdown()
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(bridge.clients, {})
        self.assertEqual(bridge._subscription_counts, {})
        self.assertIsNone(bridge.loop)
        self.assertEqual(bridge._pending, set())
        coroutine = blocked()
        bridge._schedule(coroutine)
        self.assertIsNone(coroutine.cr_frame)  # rejected coroutine is closed, not leaked

    async def test_lifespan_stops_scanner_and_finalizer(self):
        closed = threading.Event()
        closed.set()
        scanner = Mock(kws=None)
        scanner.request_stop.return_value = closed
        with patch.object(dashboard, 'scanner', scanner), patch.object(dashboard, 'bridge', dashboard.LiveBridge()):
            async with dashboard.lifespan(dashboard.app):
                await asyncio.sleep(.02)
            scanner.run.assert_called_once_with(threaded=True)
            scanner.request_stop.assert_called_once()
            self.assertTrue(dashboard.bridge._closing)

    async def test_split_snapshot_does_not_compute_unused_replay(self):
        data = SimpleNamespace(symbol='NSE:TEST')
        scanner = SimpleNamespace(symbol_data={1: data},latest_results={1: {'existing': True}},
                                  confirmed_signals={},alert_threshold=30)
        with patch.object(dashboard, 'scanner', scanner), patch.object(dashboard, 'chart_snapshot', return_value={'candles': []}):
            result = dashboard.snapshot(1)
        self.assertNotIn('indicator_history', result)
        self.assertEqual(result['strategy'], {'existing': True})


if __name__ == '__main__':
    unittest.main()
