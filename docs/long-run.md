# Long-running, low-cost autonomous execution

## Step budget

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `KARRY_MAX_TOOL_STEPS` | 100 | 1–200 | Upper bound of the ReAct loop. Always bounded. |
| `KARRY_COMPACT_EVERY` | 4 | 0–200 | Tool steps between state compactions (`0` disables). |

## State compaction

`AgentCore._run` keeps the Gemini `contents` small:

1. Every `KARRY_COMPACT_EVERY` steps, all model/function-response pairs except the latest
   `KEEP_RECENT_STEPS` (2) are replaced by one user message: the original prompt (and images)
   plus `state_digest(trace)`.
2. `state_digest` is capped at `DIGEST_CHAR_BUDGET` (4000 chars). The newest steps keep the most
   detail (arguments ≤200 chars, observation ≤600 chars); older steps collapse to
   `tool×count` counters.
3. The full `trace` (execution monitor, SSE, audit) is never truncated — only the model prompt is.

With the defaults the prompt holds at most `1 + 2 × (2 + 4 − 1) = 11` content entries regardless of
how many steps run. `AgentMetrics.compactions` reports how often this happened.

## Low-cost providers for intermediate steps

```
KARRY_LIGHT_PROVIDER=groq        # or deepseek
GROQ_API_KEY=...                 # or DEEPSEEK_API_KEY
KARRY_LIGHT_MODEL=               # optional; defaults: llama-3.3-70b-versatile / deepseek-chat
```

Routing (`karry_agent.light_llm.LightProvider`, OpenAI-compatible `/chat/completions`):

- **Gemini**: the first step (planning, multimodal input) and the final answer.
- **Light provider**: after a tool step, decides the next tool call(s) from the user request and the
  compact digest. When it answers without a tool call, Gemini writes the final answer from the digest
  with tools disabled.
- Fails closed to Gemini on missing/placeholder keys, HTTP errors, timeouts (30 s), unknown tool
  names, malformed arguments, or an exact repeat of the previous step's calls.
- Never used in private mode, secret chat, or when images are attached.
- Only read-only tools are exposed to it; tools matching
  `write|append|deploy|command|script|generate|add_|delete|create|send|publish|evolution|clone`
  stay Gemini-only.
- The request text is passed through `mask_secrets` before leaving the server.

`AgentMetrics.light_api_calls` / `light_tokens` show how much work moved off Gemini.
