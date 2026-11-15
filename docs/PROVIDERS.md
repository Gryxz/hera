# Provider adapters

Two adapters live in `src/hera/providers/`: `OpenAICompatProvider` (chat-completions JSON; covers
llama-server, a friend's endpoint, a pooled model, or a vendor) and `AnthropicProvider` (Messages
API). Both only build and parse JSON bytes. Every request goes through `Gateway.send`, so neither
imports a network library and neither ever sees an API key.

**Neither adapter has been exercised against a real provider yet.** All tests use mock servers on
127.0.0.1. Expect to fix small differences (headers, field names, error shapes) on the first real call.

## Defining destinations

A `Destination` is `name`, `url`, `headers`, `local`:

- `url` is the full endpoint URL, for example `http://127.0.0.1:8080/v1/chat/completions`. The
  adapters append nothing.
- `headers` hold the key and any required headers (OpenAI-style `Authorization: Bearer ...`;
  Anthropic `x-api-key`, `anthropic-version`, `content-type`). Load them from your local config
  or environment. Never commit them. The headers are not logged: not in the transmission log, the
  ledger, exceptions, or `repr`.
- A provider is constructed with `(name, tier, model_id, gateway, ledger, destination_name=None)`;
  `destination_name` defaults to `name` and must match a key in the gateway's destinations.
- `OpenAICompatProvider(..., send_store_false=True)` adds `"store": false` for vendor endpoints.

Each message is a `Message(role, artefact_id, data)`. For remote destinations the artefact must be
labelled and its content unchanged; HERA's own shipped prompt templates can be labelled with
`label_builtin` (never use it for repository content or model output).

## Loopback rule

A destination with `local=True` is for a model on this machine. Its URL host must be literally
`127.0.0.0/8`, `::1` or `localhost`, otherwise the gateway refuses to start. For such a
destination the gateway skips the label, grant, scanner and gate checks (the data does not leave the
machine) but still enforces the payload size limit, refuses redirects, and writes a log record with
reason `loopback` (artefact ids only, never headers or content). Segments may be unlabelled.
Use a name other than the reserved `local`. A destination with `local=False` gets every check even
if it points at 127.0.0.1; tests use that as a stand-in for a remote provider.

## Stateless use only

Each call sends the whole conversation and nothing is stored provider-side. Streaming is not
supported. Do not use thread, session or conversation-id features of any endpoint.

## Call flow and budget

`complete` builds the body, verifies it contains exactly the inspected segment text, estimates
the cost (input at 4 bytes per token plus `max_tokens` of output), asks the ledger to check caps
(a capped call raises `BudgetExceeded` and nothing is sent), sends through the gateway, then records
the real usage. A refusal raises `ProviderBlocked`; an HTTP error or unparseable reply raises
`ProviderError` (an unparseable reply also leaves a zero-usage `:unparsed` ledger row). If a reply
carries no usage, `reply.usage_reported` is `False` and zero is recorded, so check that flag.
