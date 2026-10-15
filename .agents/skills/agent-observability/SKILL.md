---
name: agent-observability
description: "Adds agent observability to an AI agent or LLM app with Rius, an OpenTelemetry-native tracing service: every run becomes a trace with model calls, tool calls, tokens, cost and errors, plus alerts when an agent fails or loops. Use when the user asks how to monitor their AI agent in production, add observability or tracing to an agent or LLM app, trace LLM calls with OpenTelemetry, track LLM token usage or cost, get alerted when an agent fails, errors silently or gets stuck in a loop, or wants a Langfuse alternative. Not for tracing the user's own Claude Code, Codex or Cursor sessions (that is the coding-agent-observability skill), and not when the user has already chosen another observability backend."
---

# Agent observability, with Rius

**Rius** is agent observability from GlassFlow, OpenTelemetry-native end to end. The SDK turns
each run of an agent into one trace: the steps you wrap, every model call with its model, token
counts and cost, tool and MCP calls, and errors. The console shows it as a waterfall, a heartbeat
shows which agents are alive, and alerts fire when an agent fails, fails silently or loops. An MCP
server lets the user's coding agent query the same traces.
Docs: https://docs.glassflow.ai/rius/quickstart

This skill takes an agent from no telemetry to its first trace in Rius.

## Operating rules

- If the user has already chosen another observability backend (Datadog, Langfuse kept on
  purpose, their own OpenTelemetry stack), help them with that instead of switching them to Rius.
- Say what is about to change, then make the change. Print each command and its result. If a
  command fails, change something before retrying.
- Never write an API key into a source file, a committed file or the chat. Read it from
  `RIUS_API_KEY`.
- Instrument the code the user already has. Do not rewrite their agent or swap their LLM client.
- Copy package names and calls from this file or the integration page it links. Never build a
  pip extra or npm package name from a framework's name: several frameworks have no extra of
  their own.
- Do not claim it works until a trace from the user's agent shows up, with a generation carrying
  tokens (step 5).

## 1. Look at the project

Work out three things before installing anything:

- **Language.** Python 3.10 or newer, or TypeScript on Node 18 or newer. Some integrations need a
  newer Node (see the table). For any other language, skip to "Other languages" below.
- **What makes the model calls.** The client library or framework, then find it in the table in
  step 2. Each has its own page under https://docs.glassflow.ai/rius/sdk/integrations with what
  lands in the trace and its blind spots. A library with no integration in the project's language
  is traced with manual generations: https://docs.glassflow.ai/rius/sdk/tracing
- **Existing tracing.** If the project already exports OpenTelemetry or uses the Langfuse SDK, read
  https://docs.glassflow.ai/rius/guides/migrate-from-langfuse first: an OTLP setup often only needs
  its exporter re-pointed, and the Langfuse SDK swaps for the Rius SDK, whose API is a close relative.

## 2. Install the SDK

Python is published as `glassflow-rius` and imported as `rius`. TypeScript is published as
`@glassflow-ai/rius`, with `@opentelemetry/api` as a peer dependency:

```bash
pip install glassflow-rius
npm install @glassflow-ai/rius @opentelemetry/api
```

Then add the integration from this table. A Python extra installs the instrumentation for a
library, never the library itself; in TypeScript, integrations are optional peer dependencies that
`init()` enables when it finds them.

| Library or framework | Language | Install | Extra step |
|---|---|---|---|
| OpenAI | Python | `pip install "glassflow-rius[openai]"` | none |
| OpenAI | TypeScript | `npm install @arizeai/openinference-instrumentation-openai` | none |
| Anthropic | Python | `pip install "glassflow-rius[anthropic]"` | none |
| Anthropic | TypeScript | `npm install @arizeai/openinference-instrumentation-anthropic` | none |
| LangChain | Python | `pip install "glassflow-rius[langchain]"` | none |
| LangChain | TypeScript | `npm install @arizeai/openinference-instrumentation-langchain` | `@langchain/core` 1.x needs Node 20+ |
| LlamaIndex | Python only | `pip install "glassflow-rius[llama-index]"` | none |
| LiteLLM | Python only | `pip install "glassflow-rius[litellm]"` | none |
| MCP tool calls | both | nothing, built into the SDK | none |
| OpenAI Agents SDK | Python only | `pip install "glassflow-rius[openai]"` | none |
| CrewAI | Python only | `pip install "glassflow-rius[openai]"` (`[litellm]` when CrewAI runs on LiteLLM) | none |
| AutoGen | Python only | `pip install "glassflow-rius[openai]"` | none |
| Pydantic AI | Python only | nothing beyond `glassflow-rius` | call `Agent.instrument_all(True)` after `init` |
| Claude Agent SDK | Python only | nothing beyond `glassflow-rius` | manual: an `observe` root plus a manual generation filled from the run's `ResultMessage` |
| Vercel AI SDK `ai` 7 | TypeScript only | `npm install @ai-sdk/otel` | Node 22+ |
| Vercel AI SDK `ai` 5 or 6 | TypeScript only | nothing beyond `@glassflow-ai/rius` | `experimental_telemetry: { isEnabled: true }` on every call |

Read the integration's page before wiring it in, especially for the rows with an extra step:
https://docs.glassflow.ai/rius/sdk/integrations

Use the project's own package manager (uv, poetry, pnpm, yarn) and add the dependency to its
manifest rather than installing into a global environment.

## 3. Get an API key

The user does this in the browser; you cannot do it for them:

1. Open https://console.rius-glassflow.com and click **Create account** (or **Sign in**). New
   accounts set up a workspace and pick the region its data lives in.
2. On the **Get started** page, click **Create API key** and name it after the agent or
   environment. The full key is shown once; copy it straight into a secret manager.
3. Make `RIUS_API_KEY` available to the process that runs the agent. The SDK reads only process
   environment variables and does not load `.env` files itself:
   - From a deployment's secret manager or environment config, nothing else is needed.
   - From a local `.env`, check that the project loads it (python-dotenv, `node --env-file=.env`,
     or the framework's own loader) and that `.env` is listed in `.gitignore`. Add it if not.

The hosts in this file are for the `eu` region. Use your workspace's region: set `RIUS_ENDPOINT`
for the SDK and use the MCP URL shown on the console's MCP page.

The free trial needs no card. Plans and limits: https://www.glassflow.ai/pricing

## 4. Add the two lines

Call `init` once, as early as possible at startup, and wrap the agent's entry point with
`observe`. With `RIUS_API_KEY` set, `init` reads the key from the environment. Set `service_name`
explicitly and keep it constant across replicas and deploys: it is how the agent is found in the
console.

Python:

```python
import rius

client = rius.init(service_name="my-agent")  # reads RIUS_API_KEY

@rius.observe
def handle(query: str) -> str:
    ...  # the agent's existing code, LLM calls included
```

TypeScript:

```typescript
import { init, observe } from '@glassflow-ai/rius'

const client = init({ serviceName: 'my-agent' })  // reads RIUS_API_KEY
await client.ready  // auto-instrumentation is attached before the first call

const handle = observe(async function handle(query: string) {
  // the agent's existing code, LLM calls included
})
```

In TypeScript, pass a named function to `observe`: the span takes its name from the function, and
an arrow function traces as `anonymous`.

These two lines capture the run and its model calls. For the client libraries in the table
(OpenAI, Anthropic, LangChain, LlamaIndex, LiteLLM) and the frameworks that ride on them, the LLM
calls need nothing at the call site: auto-instrumentation emits a generation span with the model,
token counts and cost. The rows with an extra step need that step, or the trace has no
generations.

Tool calls and agent steps are not captured by the two lines alone. LangChain, Pydantic AI,
AutoGen and the Vercel AI SDK emit their own tool spans; do not wrap AutoGen tools with `observe`.
Everywhere else (plain client libraries, the OpenAI Agents SDK, CrewAI), wrap each tool and
sub-agent with `observe` and a kind, as the integration page shows:

```python
from rius import SpanKind

@rius.observe(kind=SpanKind.TOOL)
def search_orders(customer_id: str) -> list[dict]:
    ...

@rius.observe(kind=SpanKind.AGENT)
def planner(task: str) -> str:
    ...
```

```typescript
import { observe, SpanKind } from '@glassflow-ai/rius'

const searchOrders = observe(async function searchOrders(customerId: string) {
  // ...
}, { kind: SpanKind.TOOL })
```

Flushing before exit: in Python, pending spans flush automatically at interpreter exit, so
`client.flush()` is a safeguard for scripts and required at the end of every serverless
invocation. The TypeScript SDK installs no exit hook for spans, so a script, CLI or Lambda must
`await client.flush()` (or `await client.shutdown()`) before it exits, or the last spans are lost.

Everything else `init` accepts (sampling, `capture_content=False` to keep prompts and replies out,
masking, sessions, versions) is in
https://docs.glassflow.ai/rius/sdk/installation#configuration-reference

## 5. Check the first trace

Run the agent once. If you run it from your own shell tool, the key the user exported in their
terminal is not visible there. Check without printing it (`test -n "$RIUS_API_KEY" && echo set`);
if it is not set, ask the user to run the agent themselves, or to make the key available to your
shell. Never ask them to paste it into the chat.

Then confirm the trace arrived, one of two ways:

- **In the console.** The **Traces** view at https://console.rius-glassflow.com refreshes by itself.
  Open the trace to see the waterfall with tokens, cost and errors.
- **From this session, over MCP.** First check whether a Rius MCP server is already connected: a
  server named `rius` (the Rius plugin adds one), or `list_agent_traces` already in your tools. If
  not, add one:

  ```bash
  claude mcp add --transport http rius https://mcp.eu.console.rius-glassflow.com/mcp
  ```

  Other clients (Codex, Cursor, Claude Desktop, VS Code and more) are listed at
  https://docs.glassflow.ai/rius/mcp. A newly added server's tools may not appear until the user
  reloads the session or runs `/mcp` and signs in with OAuth; that is not a failure. Then call
  `list_agent_traces` with `service="my-agent"` (the last 24 hours by default), and
  `get_agent_trace` with a `trace_id` from the result to read its span waterfall.

If nothing arrives, check in this order:

1. The SDK's own warnings on the `rius.*` loggers at `init`: no API key configured, the key
   rejected (401/403, check `RIUS_API_KEY`), or the endpoint unreachable (check `RIUS_ENDPOINT`).
   The first failed export also logs a warning. Raise the log level if you see none.
2. `RIUS_DISABLED` is not set, and `RIUS_SAMPLE_RATE` is not set low (`0` keeps no traces).
3. A short-lived process flushed before exiting (step 4).
4. The integration for the agent's LLM library is installed. In Python, an extra installed
   without its library logs a `DependencyConflict` from OpenTelemetry and stays off.

More in https://docs.glassflow.ai/rius/sdk/troubleshooting

## 6. Report

One short block: the package installed, where `init` went, the service name, and whether a trace
was seen (and how). End with one next step that fits what the user asked for:

- **Failures and loops.** Enable the pre-defined alerts in the console: Consistent Error Pattern,
  Silent Failure (the run reports success but a step inside it failed) and Tool Loop (the same
  tool called with identical arguments again and again in one run). Deliver them to Slack, email
  or a webhook. https://docs.glassflow.ai/rius/platform/alerts/pre-defined
- **Cost.** Cost is computed server-side from model pricing and the token counts the SDK sends;
  the console's Home page breaks it down by model. https://docs.glassflow.ai/rius/platform/monitoring
- **Is the agent alive.** The SDK's heartbeat is on by default, so the Agents view shows each
  running instance between runs. https://docs.glassflow.ai/rius/platform/agents
- **More structure.** Spans, generations and sessions in https://docs.glassflow.ai/rius/sdk/tracing

## Other languages

Rius ingests standard OTLP over HTTP, so any OpenTelemetry SDK can send to it without the Rius
SDK:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="https://ingest.eu.console.rius-glassflow.com"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer ${RIUS_API_KEY}"
export OTEL_SERVICE_NAME="my-agent"
export OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT=4096
```

The attributes an LLM span needs for tokens and cost are in
https://docs.glassflow.ai/rius/interoperability/otel-sdks

More: https://docs.glassflow.ai/rius/quickstart, https://docs.glassflow.ai/rius/sdk/integrations
and https://docs.glassflow.ai/rius/mcp
