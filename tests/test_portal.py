"""门户客户端协议测试：只走真实环回 HTTP，覆盖四个业务页面与故障 phase。"""
import json
import tempfile
import unittest
from pathlib import Path

from rpa import portal as P
from source_site.__main__ import running

from helpers import control


class PortalClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cm = running()
        self.base, self.source = self.cm.__enter__()
        self.addCleanup(self.cm.__exit__, None, None, None)
        self.fields = {
            "organization": "ORG-A",
            "activity_id": "DR-1",
            "date": "2026-11-09",
            "participants": "80",
            "summary_hash": "x",
        }

    def client(self, account="demo-org", password="local-only"):
        return P.PortalClient(self.base, account=account, password=password,
                              timeout=3)

    def test_login_form_submit_query_happy_path(self):
        client = self.client()
        client.login()
        self.assertEqual("ORG-A", client.organization)
        token = client.fetch_form()
        self.assertTrue(token)
        receipt = client.submit(self.fields, token)
        self.assertEqual("R1", receipt["receipt"])
        rows = client.query({"organization": "ORG-A", "activity_id": "DR-1"})
        self.assertEqual(1, len(rows))
        self.assertEqual("R1", rows[0]["receipt"])

    def test_bad_login_raises_login_failed(self):
        client = self.client(password="wrong-password")
        with self.assertRaises(P.LoginFailed) as ctx:
            client.login()
        self.assertEqual(False, ctx.exception.written)

    def test_csrf_token_is_single_use_and_written_false(self):
        client = self.client()
        client.login()
        token = client.fetch_form()
        client.submit(self.fields, token)
        # 同一令牌二次使用：明确 403、未写入
        with self.assertRaises(P.CSRFTokenInvalid) as ctx:
            client.submit(self.fields, token)
        self.assertEqual(False, ctx.exception.written)

    def test_expired_session_classified_as_session_expired(self):
        client = self.client()
        client.login()
        control(self.base, "expired")
        with self.assertRaises(P.SessionExpired):
            client.fetch_form()
        control(self.base, "base")
        # 会话自愈后可继续
        token = client.login_and_form()
        self.assertTrue(token)

    def test_business_rejection_is_explicit_not_written(self):
        client = self.client()
        client.login()
        token = client.fetch_form()
        bad = {**self.fields, "participants": "-1"}
        with self.assertRaises(P.BusinessRejected) as ctx:
            client.submit(bad, token)
        self.assertEqual(False, ctx.exception.written)
        self.assertEqual(422, ctx.exception.status)

    def test_lost_response_after_write_is_ambiguous_but_receipt_exists(self):
        control(self.base, "lost-response")
        client = self.client()
        client.login()
        token = client.fetch_form()
        with self.assertRaises(P.AmbiguousSubmission) as ctx:
            client.submit(self.fields, token)
        self.assertIsNone(ctx.exception.written)
        # 回到 base 后查询，证明记录其实已经写入
        control(self.base, "base")
        rows = self.client().query_with_session(
            {"organization": "ORG-A", "activity_id": "DR-1"})
        self.assertEqual(1, len(rows))

    def test_query_delay_returns_empty_not_an_error(self):
        client = self.client()
        client.login()
        client.fetch_form()
        control(self.base, "query-delay")
        rows = client.query({"organization": "ORG-A"})
        self.assertEqual([], rows)
        control(self.base, "base")

    def test_path_whitelist_blocks_non_business_pages(self):
        client = self.client()
        with self.assertRaises(P.PortalError):
            client._url("/__control")

    def test_second_organization_isolation(self):
        a = self.client()
        a.login()
        token = a.fetch_form()
        a.submit(self.fields, token)

        b = P.PortalClient(self.base, account="demo-org2",
                           password="local-only-2", timeout=3)
        b.login()
        self.assertEqual("ORG-B", b.organization)
        rows = b.query({"organization": "ORG-A", "activity_id": "DR-1"})
        # ORG-B 会话看不到 ORG-A 的回执
        self.assertEqual([], rows)


if __name__ == "__main__":
    unittest.main()
