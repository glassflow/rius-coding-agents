---
name: agent-observability
description: "Adds agent observability to an AI agent or LLM app with Rius, an OpenTelemetry-native tracing service: every run becomes a trace with model calls, tool calls, tokens, cost and errors, plus alerts when an agent fails or loops. Use when the user asks how to monitor their AI agent in production, add observability or tracing to an agent or LLM app, trace LLM calls with OpenTelemetry, track LLM token usage or cost, get alerted when an agent fails, errors silently or gets stuck in a loop, or wants a Langfuse alternative."
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

- Say what is about to change, then make the change. Print each command and its result. If a
  command fails, change something before retrying.
- Never write an API key into a source file, a committed file or the chat. Read it from
  `RIUS_API_KEY`; the user puts the key in their secret manager or an untracked `.env`.
- Instrument the code the user already has. Do not rewrite their agent or swap their LLM client.
- Copy package names and calls from this file or the docs it links. Do not guess at SDK APIs.
- Do not claim it works until a trace from the user's agent shows up (step 5).

## 1. Look at the project

Work out three things before installing anything:

- **Language.** Python 3.10 or newer, or TypeScript on Node 18 or newer. For any other language,
  skip to "Other languages" below.
- **What makes the model calls.** The client library or framework: OpenAI, Anthropic, LangChain,
  LlamaIndex, LiteLLM, MCP, Vercel AI SDK, OpenAI Agents SDK, Pydantic AI, CrewAI, AutoGen or
  Claude Agent SDK. Each has its own page under https://docs.glassflow.ai/rius/sdk/integrations
  with its install command and what lands in the trace.
- **Existing tracing.** If the project already exports OpenTelemetry or uses the Langfuse SDK, read
  https://docs.glassflow.ai/rius/guides/migrate-from-langfuse first: an OTLP setup often only needs
  its exporter re-pointed, and the Langfuse SDK swaps for the Rius SDK, whose API is a close relative.

## 2. Install the SDK

Python, published as `glassflow-rius` and imported as `rius`. Add an extra for each library the
agent calls; an extra installs the instrumentation for that library, never the library itself:

```bash
pip install "glassflow-rius[openai]"              # one library
pip install "glassflow-rius[anthropic,langchain]" # several
pip install "glassflow-rius[instruments]"         # everything
```

TypeScript, published as `@glassflow-ai/rius`. `@opentelemetry/api` is a peer dependency, and
integrations are optional peer dependencies you install next to it:

```bash
npm install @glassflow-ai/rius @opentelemetry/api
npm install @arizeai/openinference-instrumentation-openai      # OpenAI
npm install @arizeai/openinference-instrumentation-anthropic   # Anthropic
```

Use the project's own package manager (uv, poetry, pnpm, yarn) and add the dependency to its
manifest rather than installing into a global environment.

## 3. Get an API key

The user does this in the browser; you cannot do it for them:

1. Open https://console.rius-glassflow.com and click **Create account** (or **Sign in**). New
   accounts set up a workspace and pick the region its data lives in.
2. On the **Get started** page, click **Create API key** and name it after the agent or
   environment. The full key is shown once; copy it straight into a secret manager.
3. Set it where the agent runs: `export RIUS_API_KEY=ri_...`

The free trial needs no card. Plans and limits: https://www.glassflow.ai/pricing

## 4. Add the two lines

Call `init` once, as early as possible at startup, and wrap the functions that make up the
agent's steps. With `RIUS_API_KEY` set, `init` reads the key from the environment. Set
`service_name` explicitly and keep it constant across replicas and deploys: it is how the agent is
found in the console.

Python:

```python
import rius

rius.init(service_name="my-agent")  # reads RIUS_API_KEY

@rius.observe
def handle(query: str) -> str:
    ...  # the agent's existing code, LLM calls included
```

TypeScript:

```typescript
import { init, observe } from '@glassflow-ai/rius'

const rius = init({ serviceName: 'my-agent' })  // reads RIUS_API_KEY
await rius.ready  // auto-instrumentation is attached before the first call

const handle = observe(async function handle(query: string) {
  // the agent's existing code, LLM calls included
})
```

In TypeScript, pass a named function to `observe`: the span takes its name from the function, and
an arrow function traces as `anonymous`.

The LLM calls need nothing at the call site. Auto-instrumentation picks up every installed
integration and emits a generation span with the model, token counts and cost. A short-lived
script should flush before it exits: call `shutdown()` on the client `init` returns. Everything
else `init` accepts (sampling, `capture_content=False` to keep prompts and replies out, masking,
sessions, versions) is in https://docs.glassflow.ai/rius/sdk/installation#configuration-reference

## 5. Check the first trace

Run the agent once. Then confirm the trace arrived, one of two ways:

- **In the console.** The **Traces** view at https://console.rius-glassflow.com refreshes by itself.
  Open the trace to see the waterfall with tokens, cost and errors.
- **From this session, over MCP.** Connect the Rius MCP server (the user signs in with OAuth the
  first time a session uses it):

  ```bash
  claude mcp add --transport http glassflow https://mcp.eu.console.rius-glassflow.com/mcp
  ```

  Other clients (Codex, Cursor, Claude Desktop, VS Code and more) are listed at
  https://docs.glassflow.ai/rius/mcp. Then call `list_agent_traces` with the `service_name`
  you set, and `get_agent_trace` with a `trace_id` from the result to read its span waterfall.

If nothing arrives, check in this order: `RIUS_API_KEY` is set in the process that ran the agent;
a short-lived script called `shutdown()` before exiting; the integration for the agent's LLM
library is installed (in Python, an extra installed without its library logs a
`DependencyConflict` from OpenTelemetry and stays off). More in
https://docs.glassflow.ai/rius/sdk/troubleshooting

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
