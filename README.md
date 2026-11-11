<img src="assets/icon.svg" width="64" height="64" alt="">

# Hermes Off-Grid

A Hermes platform plugin for people with a basic phone and no data. Text a question to a shortcode, or dial a USSD code as a fallback. The reply comes back by SMS. Text commands can manage sessions, switch the model and show status.

- **Main channel: SMS.** The person texts a question. The provider posts the incoming message to this plugin's `/sms` route. The reply comes back as SMS.
- **Fallback: USSD.** The person dials a USSD code. The provider posts the session to `/ussd`. The short menu is answered on the call, and the reply comes back as SMS.
- **Both channels share one Hermes conversation per phone number.** There is no separate session per channel.
- **Scheduled results** can also be texted to one configured home number (`USSD_HOME_PHONE`).

What the person needs: a phone with cellular SMS service (and USSD for the fallback). No app install and no phone data. The server running Hermes needs internet access and a public HTTPS address that the provider can reach.

The sandbox simulator is the exception: it runs in a browser on your own computer and uses that computer's data, not the handset's.

> **Status.** Sandbox only. SMS questions and replies, HELP, STATUS, SESSIONS, MODEL listing, blocked commands, NEW + YES and RESUME were verified in the Africa's Talking sandbox. USSD questions and SMS replies were also verified there. No live carrier or physical handset has been tested. MODEL switching, /retry, /stop, /title and commands through USSD have not been live-tested. See [Known gaps](#known-gaps).

---

## Contents

1. [How it works](#how-it-works)
2. [Setup](#setup)
3. [Environment variables](#environment-variables)
4. [Demo](#demo)
5. [Limits and what gets dropped](#limits-and-what-gets-dropped)
6. [Offline tests](#offline-tests)
7. [Security](#security)
8. [Sandbox vs live](#sandbox-vs-live)
9. [Known gaps](#known-gaps)
10. [Temporary tunnel lifecycle](#temporary-tunnel-lifecycle)
11. [Project layout](#project-layout)

---

## How it works

```
SMS    phone --text--> provider --POST /sms?token=...--> hermes-ussd listener
USSD   phone --dial--> provider --POST /ussd?token=...--> hermes-ussd listener
                                                              |
                  SMS: HTTP 200 empty, no reply on the request
                  USSD: CON/END menu text, HTTP 200 immediately
                                                              |
                  accepted question --> in-memory queue --> one Hermes turn (handle_message)
                                                              |
phone <--SMS-- provider <--send()-- hermes-ussd adapter <-- reply
```

1. **SMS in.** The provider POSTs `id`, `from`, `to`, `text` to `/sms`. The plugin checks the callback token, validates the fields and the sender number, and checks the allowlist. Only then does it touch state.
2. **USSD in.** The provider POSTs `sessionId`, `phoneNumber`, `text` and related fields to `/ussd`. The plugin answers at once with a `CON` menu or an `END` screen. USSD sessions time out in seconds, so the plugin never waits for the model.
3. **Queue.** An accepted question is recorded in SQLite and placed on an in-memory queue, which one worker drains. Each item becomes a normal Hermes inbound message through `handle_message`. Only a small allowlist of Hermes commands gets gateway control (see [Commands over SMS](#commands-over-sms)); other slash commands are refused and ordinary text is chat. The model is never called from the HTTP handler.
4. **Reply.** Hermes replies through the adapter's `send()`. The reply is cleaned to plain SMS text (Markdown removed, dashes and curly quotes replaced to help use GSM-7 encoding; other Unicode letters can still require Unicode encoding) and posted to the provider as one message. The answer is stored per phone once delivery succeeds, so USSD option 2 can show it.

USSD menu, as shipped:

```
CON hermes
1. ask hermes
2. last answer
```

- `1` asks for a question. `1*<question>` queues it and ends with `END got it. answer coming by sms.`
- `2` shows the stored last answer, one screen at a time (`1. next page` to continue).
- Anything else gets `END invalid choice. dial again.`

**Conversation.** SMS and USSD messages from one phone go into the same Hermes conversation.

### Commands over SMS

SMS texts and USSD option 1 questions can run a safe subset of Hermes's own gateway commands. Everything else that looks like a command is refused with one SMS, `Command not available over SMS. Text HELP for options.`, and is neither run nor sent to the model.

| Text | Runs | What comes back |
| --- | --- | --- |
| `NEW` or `/new [name]` (`/reset`) | Hermes `/new` | Hermes asks to confirm. Reply `YES` (or `/approve`) to start a fresh chat, `NO` (or `/cancel`) to keep it. |
| `SESSIONS` or `/sessions` | `/sessions` | Up to 10 named chats from this phone |
| `RESUME <number>` or `/resume [name or number]` | `/resume` | Bare: numbered list. With a number or title: switches to it |
| `MODEL` or `/model` | `/model` | Current model plus the text list of available models (long lists are truncated) |
| `MODEL <model id>` or `/model <args>` | `/model <args> --session` | Switches for this chat only; `--global` is refused |
| `STATUS` or `/status` | `/status` | Session, model and token info |
| `/title <name>`, `/retry`, `/stop` | same | Name the chat, resend the last message, stop the running answer |
| `HELP` or `/help` | adapter text | A 214-character plain GSM-7 list of the keywords (Hermes's own `/help` lists commands SMS cannot run) |

Rules:

- Keywords are case-insensitive and only count as the **whole message**. `NEW`, `SESSIONS`, `STATUS`, `HELP` and `MODEL` must stand alone. `RESUME` takes exactly one number. `MODEL` takes exactly one model-like token (`reset`, or containing a digit or `/`, e.g. `gpt-5.5`, `openai/gpt-5`). So `new ideas for dinner?`, `resume writing tips` and `model airplanes` stay ordinary chat. To ask the model about a word that is a keyword, add any other word or punctuation (`NEW?`).
- `YES`/`Y`/`OK`/`CONFIRM`/`/approve`/`/confirm` and `NO`/`N`/`CANCEL`/`/cancel` mean confirm or cancel only in the next message within 5 minutes after `NEW`; they are sent to Hermes as `/confirm` or `/cancel`, which can only answer the `/new` prompt, never a tool approval. Otherwise they are ordinary chat (`/approve` and `/cancel` are refused). `/always` is always refused because it writes Hermes config. If an operator turned `approvals.destructive_slash_confirm` off, `/new` runs at once and a `YES` sent right after gets Hermes's "unknown command" reply.
- Refused: every other slash command, including `/yolo`, `/approve`, `/deny`, `/approvals`, `/config`, `/update`, `/tools`, `/pause`, `/sethome`, `/import`, `/export`, `/snapshot`, `/rollback`, `/bg`, `/goal`, `/loop`, `/heartbeat`, `/personality`, `/whoami`, `/history` (CLI-only in Hermes), plugin and skill commands, and unknown names. Matching mirrors Hermes's parser: leading whitespace, leading `@image:`/`@file:`/`@url:` lines, any case and `@bot` suffixes are classified; look-alike slashes (`／ ∕ ⁄ ⧸`) are treated as `/`. A `/` later in the text (`hello` then `/yolo` on the next line) is not a command to Hermes and stays chat.
- Commands count against the same per-phone rate limit and duplicate checks as questions. Adapter replies (help, refused) go through the normal SMS path and allowlist.
- Hermes has no plugin-level command allowlist. Operators can add a second layer with Hermes's own `user_allowed_commands` / `allow_admin_from` under `platforms.ussd` in `config.yaml`, but the adapter filter does not depend on it.

---

## Setup

You need:

- A Hermes installation with the gateway running and one plain model reply confirmed first.
- An Africa's Talking account with a sandbox API key, an SMS shortcode or sender, and (for the fallback) a USSD channel.
- A public HTTPS address that reaches the plugin's listener port (see [Temporary tunnel lifecycle](#temporary-tunnel-lifecycle)).

Steps:

1. **Install Hermes** and confirm the model provider answers. Provider settings belong to the Hermes host (for example `<model-provider-url>`, `<model-name>` and the provider key in `MODEL_API_KEY`). The plugin never reads `MODEL_API_KEY`.
2. **Place this plugin** where Hermes loads plugins and enable it as `hermes-ussd`. The display label is **Off-Grid**.
3. **Install the dependency** declared in the manifest (`aiohttp>=3.9,<4`):
   ```bash
   hermes plugins validate . --install-deps
   ```
4. **Set the environment** in the Hermes profile (see the table below). Never put values in this repository or a commit.
5. **Start the gateway.** The listener binds `USSD_HOST:USSD_PORT` (default `127.0.0.1:8080`) and exposes two routes: `POST /ussd` and `POST /sms`.
6. **Expose the port over HTTPS.** Then configure the provider:
   - **SMS shortcode:** set up an SMS shortcode or sender in the provider's dashboard. Set `AT_SENDER_ID` to the approved value (see below).
   - **Incoming Messages callback:** set the callback for the shortcode to
     ```
     https://<your-public-host>/sms?token=<USSD_CALLBACK_SECRET>
     ```
   - **USSD channel (fallback):** set the channel callback to
     ```
     https://<your-public-host>/ussd?token=<USSD_CALLBACK_SECRET>
     ```
   Use the same token value in both URLs, and the exact value of `USSD_CALLBACK_SECRET`.
7. **Check by texting** the shortcode from an allowlisted number, then follow the [demo](#demo).

For an SMS conversation, configure Hermes to queue follow-ups while a reply is running:

```bash
hermes config set display.busy_input_mode queue
```

Also set `HERMES_GATEWAY_BUSY_ACK_ENABLED=false` in the Hermes host environment and restart the gateway. These are **gateway-wide settings**, affecting other platforms hosted there. They avoid interrupted answers and extra busy-ack SMS that can overwrite the stored last answer. The plugin also suppresses steer-mode acknowledgments through its public display override. Both host settings are applied to the current demo.

There is **no delivery-report endpoint** in this plugin. The provider's delivery status for outbound SMS is not received or checked.

`AT_SENDER_ID`: set this to an approved **numeric shortcode** if you want people to be able to reply to the message. An alphanumeric sender ID may be accepted for sending but may not be able to receive replies. Confirm with the provider which one your account supports. If it is unset, the provider's default sender is used. The callback's `to` field is never used as the outbound sender.

Checks (run from the repository root):

```bash
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
hermes --run-module unittest discover -s tests
```

Last builder run: 72 tests passed offline, doctor passed, validate passed. Re-run after any change.

---

## Environment variables

Read through Hermes scoped secret lookup, with a fallback to platform `extra` for most names. `USSD_ALLOWED_USERS` must be set through the environment so the gateway and plugin agree.

| Name | Required | Secret | Default | Read by | Purpose |
| --- | --- | --- | --- | --- | --- |
| `AT_API_KEY` | yes | yes | none | adapter, standalone sender | Provider API key. Sent in the `apiKey` header only. |
| `AT_USERNAME` | yes | no | none | adapter, standalone sender | Provider username. Use `sandbox` for the sandbox. |
| `USSD_CALLBACK_SECRET` | yes | yes | none | adapter | Token that must match the `token` query parameter on `/ussd` and `/sms`. At least 24 characters. Compared in constant time. |
| `USSD_ALLOWED_USERS` | yes | no | none | adapter, sender | Comma-separated E.164 numbers allowed to use SMS and USSD and to receive replies. Must be non-empty. Treat as private. |
| `OFFGRID_RATE_LIMIT` | no | no | `3` | adapter | Accepted questions per phone per 60 seconds across SMS and USSD, clamped to 1–30. Current demo uses 10. |
| `AT_SENDER_ID` | no | no | provider default | adapter, sender | Approved outbound sender. Prefer a numeric shortcode that can receive replies. |
| `AT_SANDBOX` | no | no | `true` | adapter, sender | Only `false`, `0`, or `no` selects the live URL. Anything else selects sandbox. |
| `USSD_HOME_PHONE` | no | no | none | Hermes cron delivery (`cron_deliver_env_var`) | E.164 number that receives scheduled-task results by SMS. Must also be in `USSD_ALLOWED_USERS`. |
| `USSD_HOST` | no | no | `127.0.0.1` | adapter | Listener address. Keep it on loopback and let the HTTPS ingress reach it. |
| `USSD_PORT` | no | no | `8080` | adapter | Listener port. |

Not a plugin variable: `MODEL_API_KEY` (Hermes provider key, a prerequisite).

Platform ID `ussd`, plugin name `hermes-ussd` and these variable names are kept for compatibility, even though the main channel is now SMS.

---

## Demo

The demo runs on the provider's sandbox simulator in a browser on your computer. Use placeholders for every number and URL. Do not paste real numbers, codes or URLs into shared material.

**SMS path (simulator callback and reply submission confirmed):**

1. From an allowlisted number, text `<question>` to the sandbox shortcode `<SHORTCODE>`.
2. The provider posts it to `/sms`. The plugin returns an empty HTTP 200 at once, and the question goes to a normal Hermes turn.
3. The reply arrives as SMS. A long answer arrives as one message; the network bills it as up to five segments.

**USSD path (fallback, confirmed in checkpoint 1):**

1. Dial `*<SANDBOX_USSD_CODE>#` from an allowlisted number on the simulator.
2. You see:
   ```
   hermes
   1. ask hermes
   2. last answer
   ```
3. Choose `1`, then type a question. The session ends with `got it. answer coming by sms.`
4. Within a few seconds, the reply arrives as SMS in the simulator inbox.
5. Dial again and choose `2` to see the stored answer in pages.

What the demo proves: a USSD session reaches the callback, the question becomes a real Hermes turn, and the reply goes out through the sandbox API and appears in the simulator inbox.

What it does not prove: SMS inbound from a real handset, or any delivery on a real carrier. See [Sandbox vs live](#sandbox-vs-live).

---

## Limits and what gets dropped

Limits in this version:

| Item | Limit |
| --- | --- |
| Accepted questions per phone | 3 per 60 seconds by default; `OFFGRID_RATE_LIMIT` configures 1–30, shared by SMS and USSD |
| Queue | 100 items, one dispatch worker |
| Question length | 500 characters |
| USSD screen length | 180 characters |
| Request body | 12288 bytes |
| SMS fields | `id` 128, `from` 16, `to` 128, `text` 500 characters; each must appear exactly once |
| USSD fields | `sessionId` 128, `phoneNumber` 16, `text` 2048, `serviceCode` 128, `networkCode` 128 |
| Outbound answer | One message of at most 5 billed segments (about 765 plain characters, or 335 if the text needs Unicode); longer answers are cut with a ` [truncated]` marker |
| Duplicate memory | 24 hours, keyed by SMS message `id` or USSD `sessionId` with phone |
| Last answer | Kept until removed from the plugin database |

For conversational SMS, set `OFFGRID_RATE_LIMIT=10` in the Hermes environment and restart the gateway. This increases the possible SMS and model cost; it does not remove the rate limit. The current demo uses this value.

**Silent drops on the SMS path.** `/sms` always answers HTTP 200 with an empty body, so the provider does not retry. That means these cases send **no** reply and give the person **no** message:

- invalid or missing fields, or a number not in `USSD_ALLOWED_USERS`
- wrong or missing token
- queue full (busy)
- rate limited (over the configured per-minute budget)
- duplicate message `id`

**USSD** does show a short screen for busy (`END busy. please try again.`) and rate limit (`END too many questions. try later.`). Other rejections on `/ussd` return the generic `END request unavailable.`

**Accepted work is at most once.** Once a question is accepted, it lives in an in-memory queue. A crash, a restart, or a drain timeout at shutdown (about two seconds) can lose it. The person gets no reply and no error. The plugin does not retry, and it does not claim exactly-once execution.

**Long incoming texts.** Texts longer than 500 characters are silently dropped, including a concatenated four-part GSM text of roughly 612 characters. The 12288-byte request limit allows a 500-character Unicode message plus normal provider fields.

**Long answers.** A reply that would need more than five segments is shortened to fit, ending with ` [truncated]`. It is sent in a single request; delivery is not confirmed. The full cleaned answer stays available through USSD option 2. Replies that involve a web search can take 30 seconds or more before the SMS is sent.

---

## Offline tests

**Support modules, stdlib only, no network:**

```bash
python3 -m unittest discover -s tests -p "test_support_*.py"
```

These cover menu paths, SQLite state (last answer, admission, dedupe, rate limit, retention), and the SMS client (segment counting for GSM-7 and UTF-16, Markdown and punctuation cleaning, the five-segment cap, status handling, sanitized errors, no retry on ambiguous sends), using a local fake transport.

**Callback and policy tests** (`tests/test_callback_core.py`) import the Hermes gateway and need the Hermes runtime:

```bash
hermes --run-module unittest discover -s tests
```

Last builder run: 72 tests passed offline (including `tests/test_commands.py` for the command allowlist, keywords and parser bypasses), with a real local HTTP listener, queue draining, queue-full non-admission, adapter and standalone delivery, and missing-dependency registration. Provider delivery is faked. A separate runtime check also observed a provider-originated simulator SMS reaching Hermes and successful sandbox reply submission.

Do not add `tests/__init__.py`. The repository root is a package whose `__init__` imports Hermes.

---

## Security

- **Callback token.** Every request to `/ussd` and `/sms` needs `token` equal to `USSD_CALLBACK_SECRET`. Failures get a generic response with no side effects. The comparison is constant time.
- **Allowlist.** Numbers must be valid E.164 and in `USSD_ALLOWED_USERS` before any state is read. Anything else gets the same generic response, so the endpoints do not reveal which numbers exist. Hermes's own authorization still applies because messages go through `handle_message`. Trusted inbound is not enabled. Pairing and automatic enrollment are not used.
- **Gateway control only for allowlisted commands.** Plain chat has gateway control disabled. Only the commands in [Commands over SMS](#commands-over-sms) reach Hermes with control; anything that Hermes would parse as another command is answered with a fixed SMS and never dispatched. None of the allowed commands change config, approvals, tools or other people's sessions (Hermes limits `/sessions` and `/resume` to this phone's own chats unless an explicit admin is configured). The platform does not accept `update` commands.
- **Limits of the design.**
  - The callback token authenticates the caller of the callback, not the handset holding the number. The provider supplies the sender number, and the plugin trusts it.
  - The token is in the callback URL, so it appears in any proxy or provider log that records URLs. Use HTTPS, keep the token out of shared material, and rotate it if it leaks.
  - The shared SQLite database holds phone numbers, message and session identifiers, rate-limit timestamps and last answers. Protect the Hermes data directory.
- **Generic errors.** Phones see short fixed text. Malformed requests are not echoed. Provider responses and Hermes errors are not shown to phones. Logs record exception type names only.
- **Secrets.** Credentials come from the Hermes environment. The plugin does not store them in SQLite and does not log them. Access logs are disabled on the listener.
- **Outbound.** SMS goes only to allowlisted numbers and only to the fixed provider sandbox or live HTTPS host. A send is never retried automatically, because the provider may already have accepted it.

---

## Sandbox vs live

Same code. The differences are configuration and what has been verified.

| | Sandbox (demo) | Live |
| --- | --- | --- |
| `AT_SANDBOX` | `true` (default) | `false` |
| Messaging URL | `https://api.sandbox.africastalking.com/version1/messaging` | `https://api.africastalking.com/version1/messaging` |
| `AT_USERNAME` | `sandbox` | your live provider username |
| `AT_API_KEY` | sandbox key | live key |
| SMS shortcode / sender | sandbox simulator | your approved shortcode or sender |
| USSD code | sandbox channel code | a code provisioned by your provider |
| SMS destination | simulator inbox only | real handsets, carrier charges may apply |
| Verified here | USSD dial and SMS reply (checkpoint 1); SMS inbound callback and reply submission confirmed | no |

To go live: create a live key, set `AT_SANDBOX=false` and the live username, obtain and configure your approved shortcode or sender (`AT_SENDER_ID`), point the Incoming Messages callback at `/sms?token=...` and the USSD callback at `/ussd?token=...` over a stable HTTPS address. Check your provider's account for registration or approval requirements in your country. Then run a small supervised test to a number you own before anyone else uses it.

Pricing: this README makes no claim about cost. Check your provider's current price list.

---

## Known gaps

- **Sandbox only.** No live carrier or physical handset test has been run.
- **Command coverage.** MODEL switching, /retry, /stop, /title and commands through USSD have not been live-tested.
- **Provider-specific requirements may apply.** Shortcode registration or on-demand link identifiers may need extra integration; these are not verified.
- **Silent drops and queue loss.** Rate-limited and over-long SMS are dropped silently; a crash or restart can lose accepted questions. One worker handles all phones.
- **No delivery reports.** The plugin does not receive or check outbound delivery status.
- **Option 3 (tasks) is not implemented.**
- **Temporary public address.** See the next section.

---

## Temporary tunnel lifecycle

A quick tunnel is a stopgap for demos, not a deployment.

- A quick tunnel gets a random hostname. The hostname **changes** when the tunnel process restarts or is recreated.
- Every change means updating both provider callbacks (`/sms?token=...` and `/ussd?token=...`). Until then, messages and dials fail with no reply.
- No uptime is guaranteed. A restart of the host or the tunnel process can change the hostname.
- Callback URLs contain the token. Keep them in a restricted file, not in the repository, logs or shared documents.
- For anything beyond a demo, use a named tunnel or another HTTPS ingress with a stable domain, and set the callbacks once.

---

## Project layout

```
plugin.yaml          manifest: name, kind: platform, requires_env, optional_env, aiohttp dependency
__init__.py          register(ctx) entrypoint
adapter.py           platform adapter: /sms and /ussd routes, auth, allowlist, queue, send, cron sender
ussd_menu.py         pure USSD menu logic (no I/O, no Hermes imports)
at_client.py         Africa's Talking SMS client, text cleaning, encoding-aware five-segment cap
state.py             SQLite state: last answers, 24-hour dedupe, per-phone rate limit
tests/               offline tests (support modules; callback tests need Hermes)
docs/                drafts: catalog PR description, video script, X post (not published)
```
