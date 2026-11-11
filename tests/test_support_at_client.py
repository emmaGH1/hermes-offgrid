import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import at_client  # noqa: E402
from at_client import ATClient, SMSResult, clean_sms, fit_sms, sms_segments  # noqa: E402

PHONE = "+254700000001"
KEY = "super-secret-key-123"


def ok_body(n=1, status_code=101):
    recs = [{"statusCode": status_code, "number": PHONE, "status": "Success",
             "cost": "KES 0.8", "messageId": f"ATXid_{i}"} for i in range(n)]
    return json.dumps({"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": recs}}).encode()


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Scripted responses; an Exception instance is raised on post."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        r = self.responses.pop(0) if self.responses else (200, ok_body())
        if isinstance(r, Exception):
            raise r
        return FakeResp(*r)


def run(coro):
    return asyncio.run(coro)


class EncodingTests(unittest.TestCase):
    def test_segment_counts(self):
        cases = [("hello", 1), ("a" * 160, 1), ("a" * 161, 2), ("a" * 306, 2), ("a" * 307, 3),
                 ("{" * 80, 1), ("{" * 81, 2), ("\u00e9" * 160, 1),
                 ("\u4f60" * 70, 1), ("\u4f60" * 71, 2), ("\u4f60" * 134, 2), ("\u4f60" * 135, 3),
                 ("\U0001F642" * 35, 1), ("\U0001F642" * 36, 2)]
        for text, expected in cases:
            self.assertEqual(sms_segments(text), expected, text[:8])

    def test_clean_strips_markdown_and_typography(self):
        raw = "## Title\n**Hermes** \u2014 it\u2019s \u201clive\u201d\u2026\n* one\n\u2022 two\n`code` [docs](https://x.io/a)"
        self.assertEqual(clean_sms(raw),
                         "Title\nHermes - it's \"live\"...\n- one\n- two\ncode docs (https://x.io/a)")
        self.assertEqual(clean_sms(None), "")
        self.assertEqual(clean_sms("  \n"), "")

    def test_clean_drops_emoji_and_italics(self):
        self.assertEqual(clean_sms("\U0001F4CA Status\n_(note)_ snake_case_name \u2705 ok"),
                         "Status\n(note) snake_case_name ok")
        self.assertEqual(sms_segments(clean_sms("\U0001F4CA " + "a" * 150)), 1)

    def test_clean_keeps_code_like_text(self):
        for text in ("2**10 = 1024", "__init__.py", "https://x.io/a__b_c", "snake_case and a_b_c", "2*3*4"):
            self.assertEqual(clean_sms(text), text)
        self.assertEqual(clean_sms("**bold** and __strong__ and _em_"), "bold and strong and em")

    def test_clean_is_bounded_on_huge_input(self):
        import time
        start = time.monotonic()
        self.assertLessEqual(len(clean_sms("[" * 40000)), 4000)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_segments_do_not_split_escapes_or_surrogates(self):
        self.assertEqual(sms_segments("a" * 152 + "{" + "a" * 152), 3)
        self.assertEqual(sms_segments("a" * 66 + "\U0001F600" + "\u4f60" * 66), 3)

    def test_clean_keeps_real_unicode_letters_and_math(self):
        self.assertEqual(clean_sms("\u1ecd\u0301 d\u00e1ra 2 * 3"), "\u1ecd\u0301 d\u00e1ra 2 * 3")

    def test_dash_no_longer_forces_unicode(self):
        text = "Hackathon \u2014 happening now. " + "word " * 60
        self.assertGreater(sms_segments(text), sms_segments(clean_sms(text)))

    def test_fit_caps_segments_and_marks(self):
        for text in ("word " * 400, "\u4f60\u597d " * 300, "\U0001F600" * 2000, "x" * 3000):
            out = fit_sms(text)
            self.assertLessEqual(sms_segments(out), 5)
            self.assertTrue(out.endswith(" [truncated]"))
            self.assertTrue(text.startswith(out[:-len(" [truncated]")].rstrip()[:50]))
        self.assertGreater(len(fit_sms("word " * 400)), 700)
        self.assertEqual(fit_sms("a" * 765), "a" * 765)


class SendTests(unittest.TestCase):
    def client(self, *responses, **kw):
        s = FakeSession(*responses)
        return ATClient(s, api_key=KEY, **kw), s

    def test_request_shape_sandbox(self):
        c, s = self.client((201, ok_body()))
        r = run(c.send_sms(PHONE, "hi"))
        self.assertEqual(r, SMSResult(True, ("ATXid_0",), None))
        url, kw = s.calls[0]
        self.assertEqual(url, "https://api.sandbox.africastalking.com/version1/messaging")
        self.assertEqual(kw["data"], {"username": "sandbox", "to": PHONE, "message": "hi"})
        self.assertEqual(kw["headers"]["apiKey"], KEY)
        self.assertEqual(kw["headers"]["Accept"], "application/json")

    def test_sender_id_is_sent(self):
        c, session = self.client(sender_id="12345")
        self.assertTrue(run(c.send_sms(PHONE, "x" * 200)).success)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["data"]["from"], "12345")

    def test_live_host_and_username(self):
        c, s = self.client((200, ok_body()), sandbox=False)
        c2 = ATClient(s, api_key=KEY, username="myapp", sandbox=False)
        run(c2.send_sms(PHONE, "hi"))
        self.assertEqual(s.calls[0][0], "https://api.africastalking.com/version1/messaging")
        self.assertEqual(s.calls[0][1]["data"]["username"], "myapp")

    def test_long_reply_is_one_cleaned_request(self):
        text = "**Answer** \u2014 " + " ".join(f"w{i}" for i in range(100))
        c, s = self.client()
        r = run(c.send_sms(PHONE, text))
        self.assertTrue(r.success)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(s.calls[0][1]["data"]["message"], clean_sms(text))
        self.assertEqual(len(r.message_ids), 1)

    def test_long_message_is_capped_and_marked(self):
        c, s = self.client()
        self.assertTrue(run(c.send_sms(PHONE, "\u4f60" * 2000)).success)
        self.assertEqual(len(s.calls), 1)
        message = s.calls[0][1]["data"]["message"]
        self.assertTrue(message.endswith(" [truncated]"))
        self.assertLessEqual(sms_segments(message), 5)

    def test_markdown_only_text_makes_no_request(self):
        c, s = self.client()
        self.assertFalse(run(c.send_sms(PHONE, "`` ``")).success)
        self.assertEqual(s.calls, [])

    def test_http_error_not_retried_and_sanitized(self):
        c, s = self.client((401, b'{"error":"bad apiKey ' + KEY.encode() + b'"}'))
        r = run(c.send_sms(PHONE, "hi"))
        self.assertFalse(r.success)
        self.assertEqual(len(s.calls), 1)
        self.assertNotIn(KEY, repr(r))

    def test_http_200_with_rejected_recipient_is_failure(self):
        body = json.dumps({"SMSMessageData": {"Recipients": [
            {"statusCode": 403, "status": "InvalidPhoneNumber", "number": PHONE}]}}).encode()
        r = run(self.client((200, body))[0].send_sms(PHONE, "hi"))
        self.assertFalse(r.success)
        self.assertIn("InvalidPhoneNumber", r.error)

    def test_empty_recipients_and_missing_id_are_failures(self):
        empty = json.dumps({"SMSMessageData": {"Recipients": []}}).encode()
        noid = json.dumps({"SMSMessageData": {"Recipients": [{"statusCode": 101}]}}).encode()
        for body in (empty, noid):
            self.assertFalse(run(self.client((200, body))[0].send_sms(PHONE, "hi")).success)

    def test_string_status_code_accepted(self):
        r = run(self.client((200, ok_body(status_code="102"))[0:2])[0].send_sms(PHONE, "hi"))
        self.assertTrue(r.success)

    def test_garbage_bodies(self):
        for body in (b"not json", b"[]", b'{"SMSMessageData": 5}', b"{}", b"x" * 70000):
            r = run(self.client((200, body))[0].send_sms(PHONE, "hi"))
            self.assertFalse(r.success, body[:20])
            self.assertIsNotNone(r.error)

    def test_network_errors_not_retried(self):
        for exc in (asyncio.TimeoutError(), ConnectionError(f"reset {KEY}"), OSError("x")):
            c, s = self.client(exc)
            r = run(c.send_sms(PHONE, "hi"))
            self.assertFalse(r.success)
            self.assertEqual(len(s.calls), 1)
            self.assertNotIn(KEY, r.error)
        r = run(self.client(asyncio.TimeoutError())[0].send_sms(PHONE, "hi"))
        self.assertIn("unknown", r.error)

    def test_invalid_phone_and_empty_text_make_no_request(self):
        c, s = self.client()
        for phone in ("", "0700000001", "+12", "+254abc", "254700000001", "+254700000001\nx"):
            self.assertFalse(run(c.send_sms(phone, "hi")).success, phone)
        self.assertFalse(run(c.send_sms(PHONE, "   ")).success)
        self.assertEqual(s.calls, [])

    def test_constructor_validation_and_repr_hides_key(self):
        with self.assertRaises(ValueError):
            ATClient(FakeSession(), api_key="")
        self.assertNotIn(KEY, repr(ATClient(FakeSession(), api_key=KEY)))


if __name__ == "__main__":
    unittest.main()
