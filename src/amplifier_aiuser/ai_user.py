"""AIUser: an Amplifier Foundation session that drives a lower-level AI session.

The AI User is a Foundation session assembled from layered instruction:

- SYSTEM_INSTRUCTION (fixed): transport-agnostic operational rules. How to
  behave as a user, how to keep one continuous conversation going, when to
  conclude.
- Persona (per-run): who you are roleplaying. Plain string, supplied by caller.
- Scenario (per-run): what you are trying to accomplish. Plain string.
- Invocation guide (per-run): how to REACH and DRIVE the lower-level session --
  the exact commands to run, what responses look like, what "broken" looks
  like. Plain markdown string, composed by the caller.

Foundation already provides `bash`, `filesystem`, `web`, and other tools. The
AI User uses `bash` to talk to the lower-level session, exactly as the
invocation guide describes. There is no Python transport layer and no built-in
assumption about how the lower-level session is reached (a remote exec wrapper,
a local CLI, an interactive session); the caller encodes that in the invocation
guide. The LLM drives the session directly through tool calls.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from amplifier_foundation import Bundle, load_bundle

from amplifier_aiuser.tools import ConcludeResult, ConcludeTool


# Canonical bundle sources. Plain strings so the constructor can accept either
# a git URL (default, no local checkout required) or a local path override.
DEFAULT_FOUNDATION_SOURCE = "git+https://github.com/microsoft/amplifier-foundation@main"
DEFAULT_PROVIDER_SOURCE = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=providers/anthropic-sonnet.yaml"
)


SYSTEM_INSTRUCTION = """\
You are an "AI User" that drives a lower-level AI session the way a real person
would: you give it work, react to what it does, and keep it moving until the
scenario is genuinely done or the session is broken.

You have a `bash` tool. The lower-level AI session you are driving is reachable
by running shell commands. HOW to reach it and drive it -- the exact commands
to run, what its responses look like, what "broken" looks like -- is described
in the invocation guide you receive. Use `bash` together with the invocation
guide to talk to the session. Do not assume any particular transport; follow
the guide.

You will receive:

- A persona describing who you are. Stay in character.
- A scenario describing what you want to accomplish.
- An invocation guide describing how to reach and drive the lower-level session.

Use bash to drive the session according to the guide. When the scenario is done
or the session is broken, call `conclude`.

Continuity (one conversation, one session):

Unless the invocation guide says otherwise, treat the whole scenario as ONE
continuous conversation with ONE session. Every message after the first must
land in the SAME session that handled your previous messages, never a fresh
one. Different sessions are continued in different ways -- the invocation guide
tells you the mechanism for THIS one (for example: capture a session id and
pass a resume flag, or keep a single interactive session alive). Follow the
guide's continuation steps for every follow-up.

Actively watch for signs that continuity broke and the session lost the earlier
context due to issues on your end:

- It re-introduces itself, repeats first-turn setup, or re-does something you
  already did.
- It asks for something you already told it, or replies as if your earlier
  messages never happened.
- Each message appears to start a brand-new session, or a session/turn counter
  stays at 1 across multiple messages.

If you notice any of these, STOP sending scenario messages. The conversation is
not actually continuing. Investigate why (wrong command, missing or unused
session id, a new session spawned each turn), then fix your own invocation so
the next message resumes the existing session before you go on. Do NOT paper
over it by re-sending earlier messages or concatenating the whole history into
one new prompt -- that is not a real conversation. Only if the session
genuinely cannot continue despite a correct invocation is that a real finding:
conclude with verdict=failure and explain what broke.

Rules:

- Be concise. Real users do not write essays.
- If a bash command exits non-zero, hangs, or returns garbage, treat that as
  the session crashing and conclude with verdict=failure.
- Do not invent requirements beyond what the scenario states.
- Stay in role. Talk only to the lower-level session through the interface the
  invocation guide describes. Do not poke at files, processes, or anything
  outside that interface.
- The session will often return before completing the scenario. It might ask a
  clarifying question, pause for direction, or offer options without picking
  one. In every such case, send a short follow-up that nudges it to keep going
  ("go ahead", "yes", "proceed", or a brief direct answer) IF APPROPRIATE FOR
  THE SCENARIO. The scenario is "done" only when the session has actually
  attempted the task end to end, or visibly failed. Do NOT conclude
  verdict=success on a partial response.
- After conclude, do not run more bash commands and do not write a long final
  reply.
"""


DEFAULT_PERSONA = (
    "You role play as the average user who would be doing this particular scenario. You are "
    "pragmatic and outcome-oriented: you describe what you want clearly."
)


def _render_opening_prompt(
    persona: str,
    scenario: str,
    invocation_guide: str,
) -> str:
    return (
        "You are now playing this persona:\n"
        '"""\n'
        f"{persona.strip()}\n"
        '"""\n\n'
        "Scenario:\n"
        '"""\n'
        f"{scenario.strip()}\n"
        '"""\n\n'
        "How to reach and drive the lower-level AI session (its interface, the\n"
        "commands to run, what its responses and failures look like):\n"
        '"""\n'
        f"{invocation_guide.strip()}\n"
        '"""\n\n'
        "Use bash to drive the session. Call `conclude` when done."
    )


@dataclass
class InteractionResult:
    """Outcome of running the AI User against a lower-level AI session."""

    scenario: str
    persona: str
    conclude: ConcludeResult | None
    """The verdict and summary captured by the conclude tool, or None if
    the AI User never called conclude (e.g. ran out of iterations)."""

    final_assistant_text: str
    ai_user_session_id: str | None
    elapsed_s: float


class AIUser:
    """Compose Amplifier Foundation + system instruction, then run scenarios."""

    def __init__(
        self,
        foundation_source: str = DEFAULT_FOUNDATION_SOURCE,
        provider_source: str = DEFAULT_PROVIDER_SOURCE,
    ) -> None:
        """Construct an AI User.

        Args:
            foundation_source: Source for the foundation bundle. Defaults
                to the canonical git URL so no local checkout is required.
                Accepts any string `load_bundle` understands (git URL or
                local path).
            provider_source: Source for the provider bundle YAML. Defaults
                to the canonical foundation `anthropic-sonnet.yaml`. Same
                URL/path flexibility as `foundation_source`.
        """
        self.foundation_source = foundation_source
        self.provider_source = provider_source
        self._prepared = None

    async def setup(self) -> None:
        """Load + compose + prepare the bundle. Expensive; call once.

        Foundation already provides bash, filesystem, web, search, etc.
        We just compose a small system-instruction bundle on top.
        """
        foundation = await load_bundle(self.foundation_source)
        provider = await load_bundle(self.provider_source)
        system_bundle = Bundle(
            name="ai-user-system",
            version="0.1.0",
            instruction=SYSTEM_INSTRUCTION,
        )
        composed = foundation.compose(provider).compose(system_bundle)
        self._prepared = await composed.prepare()

    async def run(
        self,
        scenario: str,
        persona: str,
        invocation_guide: str,
    ) -> InteractionResult:
        """Drive the lower-level AI session through the scenario.

        Args:
            scenario: What the persona is trying to accomplish.
            persona: The character to roleplay, as a plain string. Supplied
                by the caller (for example, the product deciding the
                higher-level intent). Use DEFAULT_PERSONA if you have no
                specific persona in mind.
            invocation_guide: Markdown text describing how to REACH and DRIVE
                the lower-level session: the exact commands to run, how to keep
                one continuous session going, what responses and failures look
                like. The caller composes this however they want (read from a
                file, fetched from a database, inlined) and encodes the
                transport here -- AIUser makes no assumption about it.
        """
        if self._prepared is None:
            raise RuntimeError("AIUser.setup() must be called before run().")

        start = time.monotonic()
        conclude_tool = ConcludeTool()

        session_id = f"ai-user-{uuid.uuid4().hex[:8]}"
        session = await self._prepared.create_session(
            session_id=session_id,
            session_cwd=Path.cwd(),
        )
        await session.coordinator.mount("tools", conclude_tool, name=conclude_tool.name)

        opening = _render_opening_prompt(persona, scenario, invocation_guide)

        async with session:
            final_text = await session.execute(opening)

        return InteractionResult(
            scenario=scenario,
            persona=persona,
            conclude=conclude_tool.result,
            final_assistant_text=final_text,
            ai_user_session_id=session_id,
            elapsed_s=time.monotonic() - start,
        )
