import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ussd_menu as m  # noqa: E402
from ussd_menu import handle_ussd, paginate  # noqa: E402

PHONE = "+254700000001"


class MenuTests(unittest.TestCase):
    def test_root_for_empty_and_none(self):
        for text in ("", None, "   "):
            r = handle_ussd(PHONE, text, None)
            self.assertEqual(r.response_text, "CON hermes\n1. ask hermes\n2. last answer")
            self.assertIsNone(r.question)

    def test_ask_prompt(self):
        r = handle_ussd(PHONE, "1", None)
        self.assertEqual(r.response_text, "CON type your question")
        self.assertIsNone(r.question)

    def test_question_queued_and_stripped(self):
        r = handle_ussd(PHONE, "1*  what is the weather?  ", None)
        self.assertEqual(r.response_text, "END got it. answer coming by sms.")
        self.assertEqual(r.question, "what is the weather?")

    def test_question_keeps_everything_after_first_star(self):
        r = handle_ussd(PHONE, "1*2*3 * x", None)
        self.assertEqual(r.question, "2*3 * x")

    def test_empty_question_rejected(self):
        for text in ("1*", "1*   "):
            r = handle_ussd(PHONE, text, None)
            self.assertIsNone(r.question)
            self.assertTrue(r.response_text.startswith("END "))

    def test_question_length_boundary(self):
        ok = handle_ussd(PHONE, "1*" + "a" * 500, None)
        self.assertEqual(ok.question, "a" * 500)
        bad = handle_ussd(PHONE, "1*" + "a" * 501, None)
        self.assertIsNone(bad.question)
        self.assertTrue(bad.response_text.startswith("END "))

    def test_unknown_and_removed_options(self):
        for text in ("3", "9", "abc", "0*1", "*", "12"):
            r = handle_ussd(PHONE, text, "ans")
            self.assertTrue(r.response_text.startswith("END "), text)
            self.assertIsNone(r.question)

    def test_last_answer_missing(self):
        for ans in (None, "", "  \n "):
            r = handle_ussd(PHONE, "2", ans)
            self.assertEqual(r.response_text, "END no answer yet. ask hermes first.")

    def test_last_answer_short(self):
        r = handle_ussd(PHONE, "2", "It is sunny.")
        self.assertEqual(r.response_text, "END It is sunny.")

    def test_last_answer_paging_walks_all_pages(self):
        answer = " ".join(f"word{i}" for i in range(120))
        pages = paginate(answer)
        self.assertGreater(len(pages), 2)
        self.assertEqual(" ".join(pages), answer)
        text = "2"
        seen = []
        for i in range(len(pages)):
            r = handle_ussd(PHONE, text, answer)
            seen.append(r.response_text)
            if i < len(pages) - 1:
                self.assertTrue(r.response_text.startswith("CON "))
                self.assertTrue(r.response_text.endswith("\n1. next page"))
            else:
                self.assertTrue(r.response_text.startswith("END "))
            text += "*1"
        self.assertEqual(len(seen), len(pages))

    def test_every_screen_within_180_chars(self):
        answer = "x" * 1000 + " " + "word " * 200
        pages = paginate(answer)
        text = "2"
        for _ in pages:
            self.assertLessEqual(len(handle_ussd(PHONE, text, answer).response_text), 180)
            text += "*1"
        for t in ("", "1", "1*hi", "1*", "3", "2"):
            self.assertLessEqual(len(handle_ussd(PHONE, t, None).response_text), 180)
        self.assertLessEqual(len(m.TOO_LONG_SCREEN), 180)

    def test_unbroken_long_word_is_hard_split_without_loss(self):
        answer = "y" * 400
        self.assertEqual("".join(paginate(answer)), answer)

    def test_bad_page_input_and_overrun(self):
        answer = " ".join(f"w{i}" for i in range(100))
        n = len(paginate(answer))
        for text in ("2*2", "2*1*x", "2*", "2*1*", "2*0"):
            self.assertTrue(handle_ussd(PHONE, text, answer).response_text.startswith("END invalid"), text)
        beyond = "2" + "*1" * n
        self.assertTrue(handle_ussd(PHONE, beyond, answer).response_text.startswith("END invalid"))
        # single-page answer: any advance is out of range
        self.assertTrue(handle_ussd(PHONE, "2*1", "short").response_text.startswith("END invalid"))

    def test_deterministic_and_pure(self):
        a = handle_ussd(PHONE, "1*hello", "zzz")
        b = handle_ussd(PHONE, "1*hello", "zzz")
        self.assertEqual(a, b)
        with self.assertRaises(Exception):
            a.question = "x"  # frozen

    def test_unicode_question(self):
        r = handle_ussd(PHONE, "1*habari yako? 你好 🙂", None)
        self.assertEqual(r.question, "habari yako? 你好 🙂")


if __name__ == "__main__":
    unittest.main()
