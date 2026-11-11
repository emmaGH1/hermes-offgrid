"""Pure USSD menu logic. No storage, network, auth, logging or Hermes imports."""

from __future__ import annotations

from dataclasses import dataclass

MAX_SCREEN_CHARS = 180
MAX_QUESTION_CHARS = 500

_NEXT_SUFFIX = "\n1. next page"
# Reserve room for the "CON " prefix and the next-page instruction.
_PAGE_CHARS = MAX_SCREEN_CHARS - len("CON ") - len(_NEXT_SUFFIX)

ROOT_SCREEN = "CON hermes\n1. ask hermes\n2. last answer"
ASK_SCREEN = "CON type your question"
QUEUED_SCREEN = "END got it. answer coming by sms."
EMPTY_QUESTION_SCREEN = "END empty question. dial again to retry."
TOO_LONG_SCREEN = f"END question too long (max {MAX_QUESTION_CHARS} chars). dial again."
NO_ANSWER_SCREEN = "END no answer yet. ask hermes first."
BAD_INPUT_SCREEN = "END invalid choice. dial again."


@dataclass(frozen=True)
class MenuResult:
    response_text: str
    question: str | None = None


def paginate(answer: str) -> list[str]:
    """Split an answer into pages that fit one USSD screen (deterministic).

    Whitespace is collapsed. Breaks prefer a space within the page budget.
    """
    words_text = " ".join(answer.split())
    pages: list[str] = []
    rest = words_text
    while rest:
        if len(rest) <= _PAGE_CHARS:
            pages.append(rest)
            break
        cut = rest.rfind(" ", 0, _PAGE_CHARS + 1)
        if cut <= 0:
            cut = _PAGE_CHARS
        pages.append(rest[:cut])
        rest = rest[cut:].lstrip(" ")
    return pages


def _answer_screen(last_answer: str | None, steps: list[str]) -> str:
    pages = paginate(last_answer or "")
    if not pages:
        return NO_ANSWER_SCREEN
    # every extra step must be "1" (next page)
    if any(step.strip() != "1" for step in steps):
        return BAD_INPUT_SCREEN
    index = len(steps)
    if index >= len(pages):
        return BAD_INPUT_SCREEN
    if index == len(pages) - 1:
        return "END " + pages[index]
    return "CON " + pages[index] + _NEXT_SUFFIX


def handle_ussd(phone: str, text: str, last_answer: str | None) -> MenuResult:
    """Map cumulative USSD ``text`` to a screen and optional queued question."""
    text = (text or "").strip()
    if not text:
        return MenuResult(ROOT_SCREEN)

    head, sep, tail = text.partition("*")
    head = head.strip()

    if head == "1":
        if not sep:
            return MenuResult(ASK_SCREEN)
        question = tail.strip()
        if not question:
            return MenuResult(EMPTY_QUESTION_SCREEN)
        if len(question) > MAX_QUESTION_CHARS:
            return MenuResult(TOO_LONG_SCREEN)
        return MenuResult(QUEUED_SCREEN, question)

    if head == "2":
        steps = tail.split("*") if sep else []
        return MenuResult(_answer_screen(last_answer, steps))

    return MenuResult(BAD_INPUT_SCREEN)
