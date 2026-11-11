"""Africa's Talking SMS client. No Hermes imports; session is adapter-owned."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
LIVE_URL = "https://api.africastalking.com/version1/messaging"

GSM_SEGMENT_SEPTETS = 160
UNICODE_SEGMENT_UNITS = 70
GSM_CONCAT_SEPTETS = 153  # per segment once a message is concatenated
UNICODE_CONCAT_UNITS = 67
MAX_SEGMENTS = 5
TRUNCATED = " [truncated]"
MAX_CLEAN_CHARS = 4000
_MAX_RESPONSE_BYTES = 64 * 1024
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_OK_STATUS_CODES = {100, 101, 102}  # Processed, Sent, Queued

_GSM_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = frozenset("\f^{}\\[~]|€")


def _gsm_cost(ch: str) -> int | None:
    if ch in _GSM_BASIC:
        return 1
    if ch in _GSM_EXTENDED:
        return 2
    return None


def _utf16_cost(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def _pack(costs, single: int, per_segment: int) -> int:
    """Segments needed when no character (escape pair, surrogate pair) may straddle two."""
    if sum(costs) <= single:
        return 1
    segments, used = 1, 0
    for c in costs:
        if used + c > per_segment:
            segments, used = segments + 1, 0
        used += c
    return segments


def sms_segments(text: str) -> int:
    """Number of billed SMS segments for ``text`` in its cheapest encoding."""
    gsm = [_gsm_cost(ch) for ch in text]
    if None not in gsm:
        return _pack(gsm, GSM_SEGMENT_SEPTETS, GSM_CONCAT_SEPTETS)
    return _pack([_utf16_cost(ch) for ch in text], UNICODE_SEGMENT_UNITS, UNICODE_CONCAT_UNITS)


_MARKDOWN = (
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r"\1 (\2)"),  # [text](url)
    (re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M), ""),                     # headings
    (re.compile(r"^([ \t]*)[*\u2022][ \t]+", re.M), r"\1- "),            # bullets
    # Bold only as a matched pair around text, so 2**10 and __init__ survive.
    (re.compile(r"(?<![\w*])\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*(?![\w*.])"), r"\1"),
    (re.compile(r"(?<![\w/.])__(?=\S)([^_\n]+?)(?<=\S)__(?![\w.])"), r"\1"),
    (re.compile(r"`+"), ""),                                            # code
    (re.compile(r"(?<![\w/.])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w/])"), r"\1"),  # _italic_
    # Emoji and pictographs: one would force the whole SMS into 70-character Unicode.
    (re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u25A0-\u25FF\u21BA\u21BB\uFE0F\u200D]+ ?"), ""),
)
_PLAIN = str.maketrans({
    "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2010": "-", "\u2011": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u2022": "-", "\u00a0": " ", "\u202f": " ", "\u2192": "->",
    "\u00d7": "x", "\t": " ",
})


def clean_sms(text: str) -> str:
    """Strip Markdown and swap typographic punctuation for GSM-7 characters.

    One em dash or curly quote would otherwise force the whole SMS into the
    70-character Unicode encoding. Letters that need Unicode are left alone.
    """
    # The model can return very long text; only ~765 characters are ever sent, so
    # bound the work done on the event loop before running the regexes.
    text = (text or "")[:MAX_CLEAN_CHARS]
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    text = text.translate(_PLAIN)
    text = re.sub(r"[ ]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fit_sms(text: str, max_segments: int = MAX_SEGMENTS) -> str:
    """Return ``text`` cut to ``max_segments``, ending with a marker if cut."""
    if sms_segments(text) <= max_segments:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # segment count is monotone in prefix length
        mid = (lo + hi + 1) // 2
        if sms_segments(text[:mid].rstrip() + TRUNCATED) <= max_segments:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > lo - 40:  # prefer a word boundary when one is close
        cut = cut[:space]
    return cut.rstrip() + TRUNCATED


@dataclass(frozen=True)
class SMSResult:
    success: bool
    message_ids: tuple[str, ...] = ()
    error: str | None = None


def _safe_status(value: object) -> str:
    s = re.sub(r"[^A-Za-z0-9 _-]", "", str(value))[:40]
    return s or "unknown"


class ATClient:
    def __init__(
        self,
        session,
        *,
        api_key: str,
        username: str = "sandbox",
        sandbox: bool = True,
        sender_id: str = "",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not username:
            raise ValueError("username is required")
        self._session = session
        self._api_key = api_key
        self._username = username
        self._sandbox = sandbox
        self._sender_id = sender_id
        self._url = SANDBOX_URL if sandbox else LIVE_URL

    def __repr__(self) -> str:
        return f"ATClient(username={self._username!r}, sandbox={self._sandbox})"

    def _timeout(self):
        try:
            import aiohttp

            return aiohttp.ClientTimeout(total=15, connect=5)
        except ImportError:  # pragma: no cover - aiohttp is a declared dependency
            return None

    async def send_sms(self, phone: str, text: str) -> SMSResult:
        if not _PHONE_RE.match(phone or ""):
            return SMSResult(False, (), "invalid phone number")
        # One request: the provider concatenates segments, so the phone shows
        # a single message and there is no partial delivery.
        message = fit_sms(clean_sms(text))
        if not message:
            return SMSResult(False, (), "empty message")
        err, ids = await self._post_part(phone, message)
        return SMSResult(err is None, tuple(ids), err)

    async def _post_part(self, phone: str, message: str) -> tuple[str | None, list[str]]:
        headers = {"apiKey": self._api_key, "Accept": "application/json"}
        data = {"username": self._username, "to": phone, "message": message}
        if self._sender_id:
            data["from"] = self._sender_id
        kwargs = {"data": data, "headers": headers}
        timeout = self._timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            async with self._session.post(self._url, **kwargs) as resp:
                status = resp.status
                body = await resp.read()
        except asyncio.TimeoutError:
            return "sms request timed out (delivery unknown)", []
        except asyncio.CancelledError:
            raise
        except Exception:
            return "sms request failed (delivery unknown)", []

        if status not in (200, 201):
            return f"sms provider rejected request (http {status})", []
        if len(body) > _MAX_RESPONSE_BYTES:
            return "sms provider response too large", []
        try:
            payload = json.loads(body)
            recipients = payload["SMSMessageData"]["Recipients"]
            if not isinstance(recipients, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return "sms provider response unreadable", []
        if not recipients:
            return "sms provider accepted no recipients", []

        ids: list[str] = []
        for rec in recipients:
            if not isinstance(rec, dict):
                return "sms provider response unreadable", ids
            code = rec.get("statusCode")
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = None
            if code not in _OK_STATUS_CODES:
                return f"sms not accepted: {_safe_status(rec.get('status', code))}", ids
            mid = rec.get("messageId")
            if not mid or not isinstance(mid, str):
                return "sms provider returned no message id", ids
            ids.append(mid)
        return None, ids
