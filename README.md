# openbot-silent-run-doctor

Attribute a silent, zero-token OpenBot run to tool-array overflow — offline, from artifacts you already have.

Why now: OpenBot's `shared/model-tool-limit.ts` limits one model step to 128 tools, but when a Bot exceeds it the run still ends with `RUN_FINISHED`, zero tokens and no error in the transcript. People see "Thinking", then nothing (#883). This tool reads the AG-UI stream, the tools manifest and optionally the server log, and says whether tool overflow explains the silence — or refuses to guess.

Upstream: https://github.com/CopilotKit/OpenBot

## What upstream is and what it does not do

OpenBot is a self-hostable workspace for AI coworkers; every action is decided before it happens and recorded after. Its own source documents the 128-tool limit as **measured**, not assumed: OpenAI Chat Completions refuses `array_above_max_length` at 128, while the Responses API accepted 279 and 2,000 in the same test ([`shared/model-tool-limit.ts`](https://github.com/CopilotKit/OpenBot/blob/main/shared/model-tool-limit.ts)).

The gap is not the number — it is the **silence**. In [issue #883](https://github.com/CopilotKit/OpenBot/issues/883) a Bot with 279 tools (40 built-in + 239 Shopify actions, see [#884](https://github.com/CopilotKit/OpenBot/issues/884)) never answers: the run ends with `RUN_FINISHED`, `finishReason: "length"`, zero input/output tokens, and the only server log line is `Thread name generation returned an empty or invalid title`. The provider's 400 body is never logged or shown. Anyone holding a captured run has no way to tell "this was tool overflow" from "the agent legitimately returned nothing".

## What this tool does — and does not

Given three local artifacts:

- `--events` — the AG-UI event stream (JSONL, one event per line; `-` reads stdin),
- `--tools` — the tools manifest sent in that step (OpenAI tools array or `{"tools": [...]}`),
- `--log` — optional server log,

it returns one verdict:

| verdict | meaning |
|---|---|
| `confirmed-tool-overflow` | silent run **and** tool count over the limit **and** the provider's rejection is in the log |
| `tool-overflow-unconfirmed` | silent run and tools over the limit, but no provider rejection recorded |
| `silent-unattributed` | the run was silent but the manifest fits — **abstains instead of guessing** |
| `tool-limit-only` | the manifest is over the limit, but the run produced output |
| `healthy` | none of the above |

Rules: `RUN_FINISHED_WITHOUT_OUTPUT`, `TOOL_ARRAY_OVER_MODEL_LIMIT`, `PROVIDER_REJECTION_IN_LOG`, `TITLE_GENERATION_EMPTY` (info), `ATTRIBUTION_ABSTAIN` (warn), `RUN_UNTERMINATED`, `TOOL_NAME_INVALID`, `TOOL_NAME_DUPLICATE`.

It does **not** call any API, does not re-implement OpenBot, and does not claim the cause when the evidence is missing.

## Install / run

Requires Python 3.8+; no third-party packages.

```bash
git clone git@github-linmoxi33:linmoxi33/openbot-silent-run-doctor.git
cd openbot-silent-run-doctor
python3 bin/openbot-silent-run-doctor \
  --events examples/in/silent-overflow.events.jsonl \
  --tools  examples/in/silent-overflow.tools.json \
  --log    examples/in/silent-overflow.log
```

Real output of that command (`examples/out/silent-overflow.txt`, exit code 1):

```text
openbot-silent-run-doctor
verdict: confirmed-tool-overflow
tools: 131 (limit 128)
runs: 1
[ERROR] RUN_FINISHED_WITHOUT_OUTPUT line 3: run ended at line 3 with no text/tool events and zero measured tokens
[ERROR] TOOL_ARRAY_OVER_MODEL_LIMIT: 131 tools sent, model limit is 128 (over by 3)
[ERROR] PROVIDER_REJECTION_IN_LOG: server log carries the provider's tool-array rejection
[INFO] TITLE_GENERATION_EMPTY: thread-title generation also got an empty answer — corroborates a provider-side refusal
summary: 3 error(s), 0 warning(s), 1 info
```

`--json` (stable key order, no timestamps, byte-identical across runs):

```bash
python3 bin/openbot-silent-run-doctor --events examples/in/silent-unattributed.events.jsonl \
  --tools examples/in/small.tools.json --json
```

Exit codes: `0` no error (warnings may still be reported), `1` at least one error finding, `2` usage or input error.

Flags: `--limit N` (default 128) when your provider differs; `--json`.

## Tests

```bash
python3 tests/test_silent_run_doctor.py
```

16 stdlib `unittest` cases: 4 parsing, 11 behaviour (verdicts, exit codes, stdin ≡ file, JSON determinism, `examples/out` byte equality), 1 CLI/input-error path. All green, no network.

## Limitations

- The `128` default is OpenAI **Chat Completions**. The Responses API took 279 and 2,000 tools in upstream's own test, so a silent run at 131 tools is only `confirmed-tool-overflow` when the log actually carries the rejection; otherwise the verdict stays `tool-overflow-unconfirmed`.
- The event shapes accepted are the AG-UI ones (`RUN_STARTED`/`RUN_FINISHED`/`TEXT_MESSAGE_*`/`TOOL_CALL_*`); an unfamiliar stream yields a parse error (exit 2), not a guess.
- The examples' manifests are **synthetic fixtures** (131 minimal tools). The 128 limit, the provider error text and the title-generation log line are quoted from upstream's source and issue #883; the fixture only reproduces their shape, and the README does not claim these are OpenBot's real 279 tools.
- A silent run with a fitting manifest is reported as unattributed on purpose; this tool will not invent a cause.

## 本轮创新

First tool to treat "silent zero-token run" as a **falsifiable hypothesis** rather than a log to grep: it joins three independent artifacts (stream, manifest, log) and returns a verdict that can be wrong in a checkable way, including an explicit abstention. Existing OpenBot tools count tools; none attribute a finished-but-empty run.

## License

MIT — see `LICENSE`.
