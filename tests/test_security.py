"""脱敏与证据安全测试。"""
import unittest

from rpa.security import EvidenceStore, RedactingFilter, redact

import logging


class RedactionTests(unittest.TestCase):
    def test_redacts_password_and_session_cookie(self):
        text = (
            "POST /login account=demo-org&password=local-only "
            "Cookie: session=synthetic-orga; other=1 "
            "Set-Cookie: session=synthetic-orga; HttpOnly"
        )
        out = redact(text)
        self.assertNotIn("local-only", out)
        self.assertNotIn("synthetic-orga", out)
        self.assertIn("***REDACTED***", out)
        # 非敏感内容保留
        self.assertIn("demo-org", out)

    def test_logging_filter_redacts_record(self):
        logger = logging.getLogger("rpa.test.redaction")
        logger.handlers.clear()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Capture()
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("login password=%s session=%s", "local-only", "synthetic-orga")
        self.assertEqual(1, len(records))
        self.assertNotIn("local-only", records[0])
        self.assertNotIn("synthetic-orga", records[0])

    def test_evidence_file_is_redacted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceStore(Path(tmp))
            path = store.write("ORG-A|DR-1", "unit", {
                "headers": {"Cookie": "session=synthetic-orga"},
                "form": "account=demo-org&password=local-only",
            })
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("local-only", text)
            self.assertNotIn("synthetic-orga", text)


if __name__ == "__main__":
    unittest.main()
