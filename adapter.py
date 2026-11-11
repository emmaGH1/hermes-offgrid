"""USSD callback listener and Africa's Talking SMS gateway."""
import asyncio
import contextlib
import hashlib
import hmac
import logging
import importlib.util
import re
import time

try:
    import aiohttp
    from aiohttp import web
except ModuleNotFoundError as exc:
    if exc.name != "aiohttp":
        raise
    aiohttp = web = None
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms._shared import get_scoped_secret
from plugins.plugin_storage import plugin_db

from .at_client import ATClient, clean_sms
from .state import StateStore
from .ussd_menu import handle_ussd

logger = logging.getLogger(__name__)
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_FIELDS = {"sessionId": 128, "phoneNumber": 16, "text": 2048, "serviceCode": 128, "networkCode": 128}
_REJECT = "END request unavailable."

# Hermes gateway commands an SMS/USSD sender may run; everything else is refused (default deny).
# The sender is only a spoofable phone number, so nothing here changes config, approvals or tools.
_COMMANDS = frozenset({"new", "sessions", "resume", "model", "status", "retry", "stop", "title"})
_ALIASES = {"reset": "new"}
_SLASHES = "/\uff0f\u2215\u2044\u29f8"  # "/" and look-alikes a phone keyboard may produce
# Same leading attachment refs Hermes strips before looking for a command (MessageEvent._command_text).
_ATTACHMENT_REFS = re.compile(r"^(?:@(?:image|file|url):[^\n]+\n?)+", re.IGNORECASE)
_KEYWORDS = frozenset({"new", "sessions", "resume", "model", "status", "help"})
# MODEL <arg> is a command only for "reset" or an id-like token (has a digit or "/"): "model airplanes" is chat.
_MODEL_ARG = re.compile(r"reset\Z|(?=\S*[0-9/])[a-z0-9][a-z0-9._:/-]*[a-z0-9]\Z", re.IGNORECASE)
_CONFIRM_WORDS = {"yes": "/confirm", "y": "/confirm", "ok": "/confirm", "confirm": "/confirm",
                  "approve": "/confirm", "/confirm": "/confirm", "/approve": "/confirm",
                  "no": "/cancel", "n": "/cancel", "cancel": "/cancel", "/cancel": "/cancel"}
_CONFIRM_SECONDS = 300
HELP_TEXT = ("Offgrid: text any question to chat. Commands: NEW start a new chat (then YES "
             "to confirm), SESSIONS list chats, RESUME 2 reopen chat 2, MODEL show models, MODEL name "
             "switch, STATUS chat info, HELP this list.")
BLOCKED_TEXT = "Command not available over SMS. Text HELP for options."


def _args_allowed(name, args):
    """Argument policy for allowlisted commands; a spoofable sender gets the narrowest form.

    /resume takes only a list number (no titles: they reveal other sessions' titles,
    no --all/--cross-room). /sessions, /new and /title take no arguments (a title
    collision reports another session's id). /model may not use --global.
    """
    if name == "resume":
        return not args or args.isascii() and args.isdecimal()
    if name in ("sessions", "new", "title"):
        return not args
    if name == "model":
        return "--global" not in args.lower()
    return True


def route_text(text, awaiting_confirm=False):
    """Classify inbound text as ("chat", text), ("command", "/name args") or ("reply", sms).

    Anything Hermes would parse as a slash command is classified here, so only allowlisted
    commands ever reach Hermes with gateway control. Plain keywords count only as the whole
    message: NEW, SESSIONS, STATUS, HELP alone; RESUME <number>; MODEL alone or MODEL <model id>.
    """
    body = _ATTACHMENT_REFS.sub("", (text or "").lstrip()).lstrip()
    words = body.split()
    if awaiting_confirm and " ".join(words).lower() in _CONFIRM_WORDS:
        return "command", _CONFIRM_WORDS[" ".join(words).lower()]
    if body and body[0] in _SLASHES:
        name = words[0][1:].lower().split("@", 1)[0] if words else ""
    elif words and words[0].lower() in _KEYWORDS and (
            len(words) == 1 or len(words) == 2 and (
                words[0].lower() == "resume" and words[1].isascii() and words[1].isdecimal() or
                words[0].lower() == "model" and _MODEL_ARG.match(words[1]))):
        name = words[0].lower()
    else:
        return "chat", text
    name, args = _ALIASES.get(name, name), " ".join(words[1:])
    if name == "help":
        return "reply", HELP_TEXT
    if name not in _COMMANDS or not _args_allowed(name, args):
        return "reply", BLOCKED_TEXT
    if name == "model" and args and args.lower() != "reset":
        # Flag first, so a trailing "--provider" cannot swallow it as its value; a switch
        # never persists to config.yaml (--session beats --global).
        args = "--session " + args
    return "command", "/" + name + (" " + args if args else "")


def _setting(config, name, default=""):
    value = get_scoped_secret(name)
    return value if value is not None else (getattr(config, "extra", {}) or {}).get(name.lower(), default)


def _allowed(config):
    return {p.strip() for p in str(get_scoped_secret("OFFGRID_ALLOWED_USERS") or "").split(",") if _PHONE.fullmatch(p.strip())}


def _question_limit(config):
    try:
        return max(1, min(30, int(_setting(config, "OFFGRID_RATE_LIMIT", "3"))))
    except (TypeError, ValueError):
        return 3


def validate_config(config):
    return bool(_setting(config, "AT_API_KEY") and _setting(config, "AT_USERNAME") and
                len(str(_setting(config, "OFFGRID_CALLBACK_SECRET"))) >= 24 and _allowed(config))


def _env_enablement():
    names = ("AT_API_KEY", "AT_USERNAME", "OFFGRID_CALLBACK_SECRET", "OFFGRID_ALLOWED_USERS")
    if not all(get_scoped_secret(n) for n in names):
        return None
    return {n.lower(): get_scoped_secret(n) for n in names}


def _client(session, config):
    return ATClient(session, api_key=_setting(config, "AT_API_KEY"),
                    username=_setting(config, "AT_USERNAME"),
                    sender_id=_setting(config, "AT_SENDER_ID"),
                    sandbox=str(_setting(config, "AT_SANDBOX", "true")).strip().lower() not in {"0", "false", "no"})


async def _deliver(config, client, store, phone, content):
    if not _PHONE.fullmatch(phone or "") or phone not in _allowed(config):
        return SendResult(success=False, error="Recipient unavailable")
    try:
        result = await client.send_sms(phone, content)
        if not result.success:
            return SendResult(success=False, error="SMS delivery failed")
        store.set_last_answer(phone, clean_sms(content))
        return SendResult(success=True, message_id=result.message_ids[-1] if result.message_ids else None)
    except Exception as exc:
        logger.warning("SMS delivery failed: %s", type(exc).__name__)
        return SendResult(success=False, error="SMS delivery failed")


class USSDAdapter(BasePlatformAdapter):
    splits_long_messages = True

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("offgrid"))
        self._runner = self._session = self._store = self._worker = None
        self._queue = asyncio.Queue(maxsize=100)
        self._confirm_until = {}

    @property
    def name(self):
        return "Offgrid"

    async def connect(self, *, is_reconnect=False):
        if self._runner:
            return True
        if aiohttp is None:
            logger.warning("USSD dependency unavailable: aiohttp")
            return False
        if not validate_config(self.config):
            logger.warning("USSD configuration invalid")
            return False
        try:
            self._store = StateStore(plugin_db("offgrid"))
            self._session = aiohttp.ClientSession()
            app = web.Application(client_max_size=12288)
            app.router.add_post("/ussd", self.callback)
            app.router.add_post("/sms", self.sms_callback)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, _setting(self.config, "OFFGRID_HOST", "127.0.0.1"),
                               int(_setting(self.config, "OFFGRID_PORT", "8080")))
            await site.start()
            self._worker = asyncio.create_task(self._dispatch())
            self._mark_connected()
            return True
        except Exception as exc:
            logger.warning("USSD connection failed: %s", type(exc).__name__)
            await self.disconnect()
            return False

    async def disconnect(self):
        self._mark_disconnected()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._worker:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("USSD queue drain timed out")
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._session:
            await self._session.close()
            self._session = None
        if self._store:
            self._store.close()
            self._store = None
        self._queue = asyncio.Queue(maxsize=100)

    async def callback(self, request):
        def response(text):
            return web.Response(text=text, content_type="text/plain")
        token = request.query.get("token", "")
        secret = str(_setting(self.config, "OFFGRID_CALLBACK_SECRET"))
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            return response(_REJECT)
        try:
            form = await request.post()
            if any(len(form.getall(k, [])) > 1 or not isinstance(form.get(k, ""), str) or
                   len(form.get(k, "")) > limit for k, limit in _FIELDS.items()):
                return response(_REJECT)
            phone, session, text = (form.get(k, "") for k in ("phoneNumber", "sessionId", "text"))
            if not session or not _PHONE.fullmatch(phone) or phone not in _allowed(self.config):
                return response(_REJECT)
            result = handle_ussd(phone, text, self._store.get_last_answer(phone))
            if result.question is not None:
                if self._queue.full():
                    return response("END busy. please try again.")
                admission = self._store.admit_question("ussd:" + session, phone, time.time(), limit=_question_limit(self.config))
                if admission == "rate_limited":
                    return response("END too many questions. try later.")
                if admission == "accepted":
                    self._queue.put_nowait(("ussd:" + session, phone, result.question))
                elif admission != "duplicate":
                    return response(_REJECT)
            return response(result.response_text)
        except Exception as exc:
            logger.warning("USSD callback failed: %s", type(exc).__name__)
            return response(_REJECT)

    async def sms_callback(self, request):
        # Always acknowledge to avoid provider retry storms and paid duplicate replies.
        response = web.Response(text="", content_type="text/plain")
        secret = str(_setting(self.config, "OFFGRID_CALLBACK_SECRET"))
        token = request.query.get("token", "")
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            now = time.monotonic()
            if now - getattr(self, "_token_warning_at", float("-inf")) >= 60:
                logger.warning("SMS callback rejected: token")
                self._token_warning_at = now
            return response
        try:
            form = await request.post()
            limits = {"id": 128, "from": 16, "to": 128, "text": 500}
            if any(len(form.getall(k, [])) != 1 or
                   not isinstance(form.get(k), str) or not form[k].strip() or
                   len(form[k]) > limit for k, limit in limits.items()):
                logger.info("SMS callback rejected: fields")
                return response
            phone, message_id, question = form["from"], form["id"], form["text"].strip()
            if not _PHONE.fullmatch(phone) or phone not in _allowed(self.config):
                logger.debug("SMS callback rejected: sender")
                return response
            if self._queue.full():
                return response
            key = "sms:" + message_id
            admission = self._store.admit_question(key, phone, time.time(), limit=_question_limit(self.config))
            logger.info("SMS callback admission: %s", admission)
            if admission == "accepted":
                self._queue.put_nowait((key, phone, question))
        except Exception as exc:
            logger.warning("SMS callback failed: %s", type(exc).__name__)
        return response

    async def _dispatch(self):
        while True:
            session, phone, question = await self._queue.get()
            try:
                waiting = self._confirm_until.pop(phone, 0) > time.monotonic()
                kind, text = route_text(question, waiting)
                if kind == "reply":
                    logger.info("SMS command answered by adapter")
                    await self.send(phone, text)
                    continue
                if kind == "command":
                    name = text.split()[0][1:]
                    logger.info("SMS command: %s", name)
                    if name == "new":
                        self._confirm_until[phone] = time.monotonic() + _CONFIRM_SECONDS
                mid = hashlib.sha256((session + "\0" + phone).encode()).hexdigest()
                source = self.build_source(chat_id=phone, user_id=phone, chat_type="dm")
                await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                                                       message_id=mid, allow_gateway_control=kind == "command"))
            except Exception as exc:
                logger.warning("USSD dispatch failed: %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if not self._session or not self._store:
            return SendResult(success=False, error="Not connected")
        return await _deliver(self.config, _client(self._session, self.config), self._store, chat_id, content)

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


async def standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    if media_files or force_document:
        return {"success": False, "error": "Media is not supported"}
    if not validate_config(pconfig):
        return {"success": False, "error": "USSD configuration unavailable"}
    store = None
    try:
        store = StateStore(plugin_db("offgrid"))
        async with aiohttp.ClientSession() as session:
            result = await _deliver(pconfig, _client(session, pconfig), store, chat_id, message)
        return {"success": result.success, **({"message_id": result.message_id} if result.success else {"error": result.error})}
    except Exception as exc:
        logger.warning("USSD standalone delivery failed: %s", type(exc).__name__)
        return {"success": False, "error": "SMS delivery failed"}
    finally:
        if store:
            store.close()


def register(ctx):
    ctx.register_platform(name="offgrid", label="Offgrid", dashboard_description="SMS conversations with USSD fallback",
                          adapter_factory=USSDAdapter, check_fn=lambda: importlib.util.find_spec("aiohttp") is not None,
                          install_hint="Install the declared aiohttp dependency.",
                          validate_config=validate_config, is_connected=validate_config,
                          required_env=["AT_API_KEY", "AT_USERNAME", "OFFGRID_CALLBACK_SECRET", "OFFGRID_ALLOWED_USERS"],
                          env_enablement_fn=_env_enablement, allowed_users_env="OFFGRID_ALLOWED_USERS",
                          cron_deliver_env_var="OFFGRID_HOME_PHONE", standalone_sender_fn=standalone_send,
                          max_message_length=0, display_tier="minimal", pii_safe=True,
                          display_overrides={"busy_steer_ack_enabled": False},
                          allow_update_command=False, allowlist_suppresses_pairing=True,
                          platform_hint="You are chatting through Offgrid over SMS, with an optional USSD fallback. Do not guess the model name. Reply in plain text with no Markdown, emoji or special symbols. Keep replies short: aim for under 450 characters; anything past about 750 is cut off.")
