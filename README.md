# amplifier-bundle-aiuser

A reusable **AI User**: a layer between higher-level human intent (a person
decides the task and persona) and a continuing lower-level AI session that needs
to be kept going.

The AI User is an Amplifier Foundation session that role-plays a human. Given a
persona, a scenario, and an invocation guide, it drives a lower-level AI session
across multiple turns until the scenario is genuinely done or the session is
broken, then reports a verdict.

It is designed to be embedded in other products: anywhere you need an autonomous
stand-in for a human user to keep a lower-level session moving. For example, a
pipeline node that drives an agent toward a goal, or automated exercising of an
agent that expects a real user at the other end. Import it and drive it directly.

## Install

```bash
uv sync
```

`amplifier-core` and `amplifier-foundation` are sourced from sibling checkouts
(see `[tool.uv.sources]` in `pyproject.toml`).

## Usage

```python
from amplifier_aiuser import AIUser

ai_user = AIUser()        # defaults to amplifier-foundation + anthropic-sonnet
await ai_user.setup()     # expensive: loads + composes + prepares. Call once.

result = await ai_user.run(
    scenario="What you want the lower-level session to accomplish.",
    persona="Who you are roleplaying (the caller decides this).",
    invocation_guide="How to reach and drive the lower-level session: the exact "
                     "commands to run, how to keep one session going, what its "
                     "responses and failures look like.",
)

if result.conclude is not None:
    print(result.conclude.verdict)   # success | partial | failure | give_up
    print(result.conclude.summary)
```

## The contract

```python
async def run(scenario: str, persona: str, invocation_guide: str) -> InteractionResult
```

- `scenario` and `persona` are the higher-level human intent, supplied by the
  caller.
- `invocation_guide` carries the transport. AIUser makes no assumption about how
  the lower-level session is reached (a remote exec wrapper, a local CLI, an
  interactive session); the caller encodes that here as plain markdown.
- One-shot autonomous: `run()` drives the whole interaction internally and
  returns when the AI User calls `conclude` (or stops without concluding, in
  which case `result.conclude is None`).

`InteractionResult` fields: `scenario`, `persona`, `conclude` (verdict +
reasoning + summary, or `None`), `final_assistant_text`, `ai_user_session_id`,
`elapsed_s`.

## Status

Library-only today. The bundle manifest (`bundle.md`) is a thin identity shell;
the AI User composes its own Foundation session at runtime. Evaluations for this
bundle live in `.amplifier/evaluations/` (run them with
`.amplifier/evaluations/run.sh`).
