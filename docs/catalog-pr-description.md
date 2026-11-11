# Draft catalog PR description

## Add offgrid (Offgrid)

Offgrid is a platform plugin for chatting with a Hermes agent by SMS without phone data. Text commands support sessions, model selection and status. A USSD menu can submit a question for an SMS answer or page through the last answer. Both channels share a conversation per phone. Scheduled results can be sent to a configured home number.

The plugin is named `offgrid`, registers the `offgrid` platform and uses `OFFGRID_*` and `AT_*` environment variables, so it can be installed alongside the existing `ussd-platform` catalog plugin. It uses Africa's Talking, aiohttp and SQLite. Callback tokens and an E.164 allowlist protect access; the callback token authenticates the provider request, not the handset. Only an allowlisted subset of gateway commands is accepted.

Africa's Talking receives phone numbers and message text; the configured model provider receives questions. SQLite stores phone numbers, identifiers, rate-limit timestamps and last answers. SMS and USSD may incur charges. There are no delivery reports. One worker serves all phones; queued work can be lost on a crash, and rate-limited or over-long SMS are silently dropped. Replies are sent as one message, capped at five billed segments.

## Testing

72 offline tests, plugin doctor and plugin validation passed. SMS questions and replies, HELP, STATUS, SESSIONS, MODEL listing, blocked commands, NEW + YES and RESUME were verified in the sandbox, as was USSD-to-SMS. MODEL switching, /retry, /stop, /title and commands through USSD have not been live-tested. No live carrier or physical handset test has been run.

## Source

Repository: https://github.com/emmaGH1/hermes-offgrid

Pinned commit: `<full commit SHA>` (to be filled after publication)
