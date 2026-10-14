# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import functools
import importlib.util
import pathlib
import socket
import threading
from unittest import mock

from keystoneauth1 import exceptions
from keystoneauth1 import session
from urllib3.util import retry

from tests.unit import test


PLUGIN = (pathlib.Path(__file__).resolve().parents[2]
          / "ultimum" / "plugins" / "connect_retries.py")


@functools.cache
def load_plugin():
    """Import the plugin, keeping TCPKeepAliveAdapter unpatched here."""
    original = session.TCPKeepAliveAdapter.__init__
    original_send = session.TCPKeepAliveAdapter.send
    spec = importlib.util.spec_from_file_location(
        "ultimum_connect_retries_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session.TCPKeepAliveAdapter.__init__ = original
    session.TCPKeepAliveAdapter.send = original_send
    return module


def closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConnectRetriesTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()
        patcher = mock.patch.object(session.TCPKeepAliveAdapter, "__init__",
                                    self.module.adapter_init)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(session.TCPKeepAliveAdapter, "send",
                                    self.module.adapter_send)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No real waiting between attempts.
        sleep = mock.patch.object(retry.Retry, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def env(self, value):
        return mock.patch.dict(self.module.os.environ,
                               {"ULTIMUM_RALLY_CONNECT_RETRIES": value})

    def test_disabled_keeps_the_default(self):
        with mock.patch.dict(self.module.os.environ, clear=True):
            adapter = session.TCPKeepAliveAdapter()
        self.assertEqual(0, adapter.max_retries.total)

    def test_only_connection_errors_are_retried(self):
        with self.env("4"):
            adapter = session.TCPKeepAliveAdapter()
        policy = adapter.max_retries
        self.assertEqual((4, 4, False, 0, 0, 0),
                         (policy.total, policy.connect, policy.read,
                          policy.redirect, policy.status, policy.other))

    def test_invalid_value_disables_retries(self):
        with self.env("many"):
            adapter = session.TCPKeepAliveAdapter()
        self.assertEqual(0, adapter.max_retries.total)

    def test_connect_timeout_counts_as_a_connection_error(self):
        from urllib3 import exceptions as urllib3_exceptions

        policy = self.module.connect_only_retry(2)
        policy = policy.increment(
            method="POST", url="/servers",
            error=urllib3_exceptions.ConnectTimeoutError("timed out"))
        self.assertEqual(1, policy.connect)

    def test_refused_connection_is_attempted_again(self):
        attempts = []
        original = retry.Retry.increment

        def increment(policy, *args, **kwargs):
            attempts.append(kwargs.get("error"))
            return original(policy, *args, **kwargs)

        with self.env("3"), mock.patch.object(retry.Retry, "increment",
                                              increment):
            sess = session.Session()
            self.assertRaises(exceptions.ConnectFailure, sess.get,
                              "http://127.0.0.1:%d/" % closed_port())
        # The first attempt and three retries all failed to connect.
        self.assertEqual(4, len(attempts))

    def test_read_timeout_is_not_retried(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        self.addCleanup(listener.close)
        accepted = []

        def accept():
            # Accept the request but never answer it.
            while True:
                try:
                    accepted.append(listener.accept()[0])
                except OSError:
                    return

        threading.Thread(target=accept, daemon=True).start()
        with self.env("3"):
            sess = session.Session(timeout=0.5)
            self.assertRaises(
                exceptions.ConnectTimeout, sess.get,
                "http://127.0.0.1:%d/" % listener.getsockname()[1])
        for conn in accepted:
            conn.close()
        self.assertEqual(1, len(accepted))


class ConnectTimeoutTestCase(test.TestCase):
    def setUp(self):
        super().setUp()
        self.module = load_plugin()

    def env(self, value):
        return mock.patch.dict(self.module.os.environ,
                               {"ULTIMUM_RALLY_CONNECT_TIMEOUT": value})

    def test_total_timeout_becomes_the_read_timeout(self):
        with self.env("10"):
            self.assertEqual((10.0, 180.0),
                             self.module.split_timeout(180.0))
            self.assertEqual((10.0, None), self.module.split_timeout(None))
            # Never longer than the whole request may take.
            self.assertEqual((0.5, 0.5), self.module.split_timeout(0.5))
            # Callers that split it themselves keep their values.
            self.assertEqual((3, 60), self.module.split_timeout((3, 60)))

    def test_disabled_or_invalid_keeps_the_timeout(self):
        for value in ("0", "", "soon"):
            with self.env(value):
                self.assertEqual(180.0, self.module.split_timeout(180.0))

    def test_session_requests_get_the_connect_timeout(self):
        sent = []

        def send(adapter, request, **kwargs):
            sent.append(kwargs["timeout"])
            raise RuntimeError("stop")

        with self.env("10"), \
                mock.patch.object(self.module, "upstream_send", send), \
                mock.patch.object(session.TCPKeepAliveAdapter, "send",
                                  self.module.adapter_send):
            sess = session.Session(timeout=180)
            self.assertRaises(RuntimeError, sess.get, "https://cloud:5000/")
        self.assertEqual([(10.0, 180.0)], sent)
