"""来源样本和本地HTTP服务的回归检查。"""
import hashlib
import json
import unittest
from urllib.request import urlopen
from source_site.source import Source
from source_site.__main__ import running


class SourceTests(unittest.TestCase):
    def test_source_1(self):
        s = Source()
        self.assertEqual(401, s.request("GET", "/form")[0])

    def test_source_2(self):
        s = Source()
        self.assertEqual(401, s.request("POST", "/login", body=b"account=x&password=x")[0])

    def test_source_3(self):
        s = Source()
        self.assertEqual(200, s.request("POST", "/login", body=b"account=demo-org&password=local-only")[0])

    def test_source_4(self):
        s = Source()
        self.assertEqual(403, s.request("POST", "/submit", headers={"cookie": "session=synthetic"}, body=b"csrf=bad")[0])

    def test_http_source(self):
        from urllib.request import Request
        with running() as (base, source):
            request = Request(base + "/login", data=b"account=demo-org&password=local-only")
            with urlopen(request, timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertIn("session=", response.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
