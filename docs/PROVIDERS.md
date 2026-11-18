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
the cost (input at 3 bytes per token, which over-counts on purpose, plus `max_tokens` of output), asks
the ledger to check caps (a capped call raises `BudgetExceeded` and nothing is sent), sends through
the gateway, then records the real usage. A privacy refusal raises `ProviderBlocked`. An HTTP error,
an unparseable reply, or a transport failure (connection refused, timeout, reset: the gateway allowed
the send but no response came back) raises `ProviderError`, so the recovery ladder treats it as a
provider failure and never as a privacy block. After a transport failure the request may still have
reached the provider and been billed, so the ledger is charged for it: the gateway sets
`SendResult.may_have_arrived`, which is `False` only when the failure certainly came before any
request was complete (connection refused, name resolution failure, TLS handshake or certificate
failure). urllib wraps in `URLError` only what is raised while connecting and sending, so a TLS error
counts as "not arrived" only when it came wrapped; a bare one (raised while reading the reply) came
after the whole request was sent. Everything else counts as "may have arrived": timeouts (a connect and a read timeout look alike), resets, disconnects, short reads, broken
pipes and any unknown exception. In that case, on a non-`local` tier, the provider records one row
costing the pre-call estimate (the figure the cap check used) with purpose
`<purpose>:transport_error:estimated`, then raises `ProviderError`. The estimate is conservative (an
over-count on purpose: a full `max_tokens` of output is assumed), so later cap checks see the possible
spend. Nothing is recorded when `may_have_arrived` is `False` or on the `local` tier. An unparseable reply
leaves a zero-usage `:unparsed` ledger row. If a reply
carries no usage, `reply.usage_reported` is `False` and zero is recorded, so check that flag.

## Rules enforced in code

- A remote destination must use `https`. Plain `http` is accepted only for loopback addresses.
- The `local` tier is free and exempt from spending caps, so a provider on the `local` tier must point at a destination marked `local` (a loopback address), and the reverse.
- If a paid provider's reply carries no token usage, the call is recorded with a size-based estimate and its purpose is suffixed `:estimated`. It is never recorded as free.

## Grok (xAI)

`GrokProvider` uses xAI's OpenAI-compatible, stateless chat-completions endpoint: URL `https://api.x.ai/v1/chat/completions`, header `Authorization: Bearer <key>`. Differences from the generic adapter: it always sends `max_completion_tokens`, drops `stop`, and counts reasoning tokens as output so reasoning is never billed as free. Like every remote provider it is a remote destination: only public or explicitly approved content may be sent, and xAI's data-retention terms have not been reviewed for this project. Tested against a mock server only.

## A supervised local model host

With `[local_model].command` set (see [MODELHOST.md](MODELHOST.md)), every provider of tier `local`, kind
`openai_compat` and URL exactly `http://127.0.0.1:<[local_model].port>` carries a `ModelHost`
(`provider.host`). Its `complete()` then takes a lease around the call (one local generation at a time),
refuses a prompt whose estimated tokens (request bytes / 3) exceed the lease's context cap before anything is
sent or recorded, sends `chat_template_kwargs: {"enable_thinking": false}` unless the caller passed its own,
and reports `HostUnavailable`, `HostStartError` and `HostBusy` as `ProviderError`. A local provider on that
port without a command is unchanged; a command with no such provider is shown by `hera providers list`.
