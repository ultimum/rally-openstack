"""Retry OpenStack API calls whose TCP connection could not be opened.

A flaky path to the cloud (e.g. a VPN) makes connects time out now and then
("Connection to ... timed out"), which fails whole workloads before any
request reached the cloud. All Rally clients talk through keystoneauth
sessions, which send through ``TCPKeepAliveAdapter``. While
``ULTIMUM_RALLY_CONNECT_RETRIES`` is a positive number, that adapter retries
**only connection errors**: the host could not be resolved or the TCP
connection could not be opened (refused or timed out), so nothing was sent.
Read timeouts, dropped connections, TLS/certificate errors and HTTP error
responses are not retried, so no API call can run twice; they are raised
exactly as without this module. Waits between attempts grow 4, 8, 16, ...
seconds (at most 120).
"""

import os

from keystoneauth1 import session
from urllib3.util import retry


upstream_init = session.TCPKeepAliveAdapter.__init__


def connect_retries():
    try:
        return max(0, int(os.environ.get("ULTIMUM_RALLY_CONNECT_RETRIES")
                          or 0))
    except ValueError:
        return 0


def connect_only_retry(retries):
    # read=False re-raises read errors unchanged, like requests' default.
    return retry.Retry(
        total=retries, connect=retries, read=False, redirect=0, status=0,
        other=0, backoff_factor=2, raise_on_status=False)


def adapter_init(self, *args, **kwargs):
    upstream_init(self, *args, **kwargs)
    retries = connect_retries()
    if retries:
        self.max_retries = connect_only_retry(retries)


session.TCPKeepAliveAdapter.__init__ = adapter_init
