"""
test_outbound_dns.py — guards the IPv4-only outbound DNS setting in engine.py.

Why this is worth a test: every timeout we hand `requests` covers connect and
read, but NOT name resolution. `socket.getaddrinfo` is a blocking C call and no
Python-level timeout can interrupt it, so a stalled lookup hangs the calling
thread for as long as the resolver takes. Captured from the production worker via
/api/diag/stacks while the service was wedged:

    engine.py in _detect_objects_gemini -> _GEMINI_HTTP_SESSION.post(...)
    urllib3/util/connection.py in create_connection -> socket.getaddrinfo(...)
    socket.py in getaddrinfo

One hung lookup costs one of the worker's four threads, and a couple of them take
the whole service down. Restricting resolution to IPv4 drops the AAAA leg that
stalls on a container with no IPv6 route. If someone later removes the call or
the urllib3 hook is renamed upstream, this fails loudly instead of quietly
handing back the unbounded hang.

Run with:
    python3 -m pytest test_outbound_dns.py -v
"""

import socket
import unittest
from unittest.mock import patch

import urllib3.util.connection as urllib3_connection

import engine


class TestOutboundDnsIsIPv4Only(unittest.TestCase):

    def test_importing_engine_restricts_resolution_to_ipv4(self):
        self.assertEqual(
            urllib3_connection.allowed_gai_family(), socket.AF_INET,
            "engine must restrict outbound DNS to IPv4 — an AAAA lookup on a "
            "host with no IPv6 route stalls in getaddrinfo, which no request "
            "timeout can interrupt",
        )

    def test_the_switch_is_honored(self):
        """FORCE_IPV4_DNS=0 leaves resolution alone, for a host where IPv6 works."""
        original = urllib3_connection.allowed_gai_family
        try:
            sentinel = object()
            urllib3_connection.allowed_gai_family = sentinel
            with patch.dict("os.environ", {"FORCE_IPV4_DNS": "0"}):
                engine._force_ipv4_dns()
            self.assertIs(urllib3_connection.allowed_gai_family, sentinel,
                          "FORCE_IPV4_DNS=0 must leave the resolver untouched")
        finally:
            urllib3_connection.allowed_gai_family = original

    def test_applying_it_is_idempotent(self):
        engine._force_ipv4_dns()
        engine._force_ipv4_dns()
        self.assertEqual(urllib3_connection.allowed_gai_family(), socket.AF_INET)

    def test_outbound_requests_still_resolve(self):
        """The point is to remove a hang, not connectivity."""
        addrs = socket.getaddrinfo("generativelanguage.googleapis.com", 443,
                                   urllib3_connection.allowed_gai_family(),
                                   socket.SOCK_STREAM)
        self.assertTrue(addrs, "the Gemini endpoint must still resolve over IPv4")
        for family, *_rest in addrs:
            self.assertEqual(family, socket.AF_INET)


if __name__ == "__main__":
    unittest.main()
