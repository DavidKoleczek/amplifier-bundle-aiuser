The agent is the Amplifier CLI with the amplifier-foundation bundle composed
(it has bash, filesystem, and web tools). It is an ordinary interactive coding
agent. There is no special mode to activate.

You must drive ONE long-lived interactive TUI and send every message into that
same session, so the agent remembers earlier turns. You keep the one TUI alive
across your separate calls with `tmux`.

## Start the agent once (persistent tmux session)

Launch the TUI inside a detached tmux session named `agent`:

    tmux kill-server 2>/dev/null; tmux new-session -d -s agent -x 220 -y 50 'cd /workspace && amplifier'

The TUI is slow to start (allow 15-25 seconds). Poll the screen until the input
prompt `>` appears at the bottom before sending anything:

    tmux capture-pane -p -t agent

You will see a session banner that ends in a `>` prompt. A warning that the
terminal does not support cursor position requests (CPR) is harmless; ignore it.

## Send each message into the same session

Type a message and submit it with Enter:

    tmux send-keys -t agent '<your message>' Enter

Responses take 20-90 seconds. Poll the screen until the `>` prompt returns at
the bottom with no `Thinking...`/`Working...` spinner, which means the reply is
complete:

    tmux capture-pane -p -t agent

To read a long reply that scrolled past the visible screen, capture scrollback
as well:

    tmux capture-pane -p -t agent -S -400

For a message with tricky quoting (quotes, newlines), write it to a file and
type it from there instead of inlining it:

    echo '<your message>' > /tmp/msg.txt
    tmux send-keys -t agent "$(cat /tmp/msg.txt)" Enter

## Letting the agent work

This agent can write and run code itself through its own tools. You do not run
the agent's code for it; you ask it, in plain language, to build what you want,
to run it, and to show you the result. If it pauses, asks a clarifying question,
or stops after a partial step, send a short follow-up that nudges it to keep
going ("go ahead", "yes, run it", "show me the output").

## Continuity (critical)

Every follow-up MUST go to the SAME `tmux ... -t agent` session. Do NOT run
`amplifier` again, do NOT start a second tmux session, and do NOT use
`amplifier run` for follow-ups. Each of those starts a fresh agent session with
no memory and silently breaks the conversation. One scenario = one tmux `agent`
session from start to finish. The `>` prompt staying put and the agent
remembering earlier turns are how you confirm you are still in the same session.
