"""Evaluation runner for the standalone AIUser.

This is a thin custom harness. Unlike the standard amplifier_evaluation trial
flow -- where AIUser is the driver and the agent-under-test is graded -- here the
standalone AIUser ITSELF is the subject. We reuse the evaluation building blocks
(DTU, install_agent, compose_launch_profile, Grader) and swap in
`amplifier_aiuser.AIUser` as the thing under test.

Per scenario:
  1. compose_launch_profile(agent, task.profile) -> merged profile
  2. DTU.launch(merged)                            (lower-level session host)
  3. install_agent(stock-amplifier, dtu)           (the session AIUser drives)
  4. seed task workspace files into /workspace
  5. AIUser.run(scenario, persona, invocation_guide)   <-- SUBJECT
  6. Grader.run(grader.yaml, ..., dtu.id)          (reused building block)
  7. combine verdict + grader score -> PASS / FAIL / INCONCLUSIVE
  8. destroy DTU; write outputs under
     <workspace>/.amplifier/evaluation/aiuser/<run-id>/<task-id>/

Run output is NOT source controlled (it can contain prompts, responses, paths).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from amplifier_aiuser import AIUser
from amplifier_evaluation.grader import Grader
from amplifier_evaluation.harness.dtu import DTU, cli_available
from amplifier_evaluation.harness.install import compose_launch_profile, install_agent
from amplifier_evaluation.harness.loaders import load_agent, load_task

THIS_DIR = Path(__file__).resolve().parent  # <repo>/.amplifier/evaluations
REPO_ROOT = THIS_DIR.parent.parent  # <repo> (amplifier-bundle-aiuser)
WORKSPACE_ROOT = REPO_ROOT.parent  # the surrounding workspace
AGENTS_DIR = THIS_DIR / "agents"
TASKS_DIR = THIS_DIR / "tasks"

GIT_FOUNDATION = "git+https://github.com/microsoft/amplifier-foundation@main"
GIT_PROVIDER = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=providers/anthropic-sonnet.yaml"
)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _resolve_sources() -> tuple[str, str]:
    """Prefer local sibling checkouts (fast, offline); fall back to git URLs."""
    foundation_local = WORKSPACE_ROOT / "amplifier-foundation"
    provider_local = foundation_local / "providers" / "anthropic-sonnet.yaml"
    foundation = os.environ.get("AIUSER_EVAL_FOUNDATION_SOURCE") or (
        str(foundation_local) if foundation_local.is_dir() else GIT_FOUNDATION
    )
    provider = os.environ.get("AIUSER_EVAL_PROVIDER_SOURCE") or (
        str(provider_local) if provider_local.is_file() else GIT_PROVIDER
    )
    return foundation, provider


def _transport_preamble(dtu_id: str, workspace_dir: str = "/workspace") -> str:
    return (
        f"The lower-level AI session runs inside a Digital Twin Universe container\n"
        f"with id `{dtu_id}`. You reach it ONLY by running shell commands through\n"
        f"the exec wrapper:\n\n"
        f"    amplifier-digital-twin exec {dtu_id} -- bash -c 'cd {workspace_dir} && <command>'\n\n"
        f"So every command in the guide below (including each `tmux ...` command)\n"
        f"must be wrapped exactly like that, for example:\n\n"
        f"    amplifier-digital-twin exec {dtu_id} -- bash -c 'cd {workspace_dir} && tmux capture-pane -p -t agent'\n\n"
        f"The agent's working directory is `{workspace_dir}` and the scenario's seed\n"
        f"files are already there. The guide below describes the agent's CLI assuming\n"
        f"you are already inside `{workspace_dir}`; wrap each of its commands with the\n"
        f"exec pattern above to actually run it.\n"
    )


def _grader_context(instructions: str, result, conversation: str) -> str:
    """Build the grader's task_context.

    The grader's primary material is the ACTUAL conversation transcript (so it can
    analyze how the AI User drove the session and where it ended). The AI User's
    own conclude verdict/summary is included only as a labelled, secondary
    reference -- graders are instructed not to grade on the verdict label.
    """
    parts = [instructions]

    convo = conversation.strip()
    if convo:
        max_len = 70000
        if len(convo) > max_len:
            half = max_len // 2
            convo = (
                convo[:half]
                + "\n\n...[conversation truncated in the middle]...\n\n"
                + convo[-half:]
            )
        parts.append(
            "\n\n--- Conversation transcript (the lower agent's session as the AI User drove it) ---\n"
            + convo
            + "\n--- end conversation transcript ---\n"
        )

    if result is not None and result.conclude is not None:
        parts.append(
            "\n\n--- AI User's own self-reported outcome (reference only; do NOT grade on the verdict label) ---\n"
            f"verdict: {result.conclude.verdict}\n"
            f"reasoning: {result.conclude.reasoning}\n"
            f"summary: {result.conclude.summary}\n"
        )
    elif result is not None:
        parts.append(
            "\n\n--- AI User's own self-reported outcome (reference only) ---\n"
            "(the AI User never called conclude)\n"
        )

    return "".join(parts)


async def run_scenario(
    task, agent, ai_user: AIUser, grader: Grader, out_dir: Path
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "task_id": task.id,
        "dtu_id": None,
        "verdict": None,
        "expected_verdict": task.meta.get("expected_verdict", []),
        "grader_score": None,
        "outcome": "ERROR",
        "error": None,
    }

    merged = compose_launch_profile(
        agent, task.profile_path, out_dir / "launch_profile.yaml"
    )

    _log(f"[{task.id}] launching DTU ...")
    dtu = await DTU.launch(merged)
    record["dtu_id"] = dtu.id
    _log(f"[{task.id}] DTU {dtu.id} up")

    try:
        _log(
            f"[{task.id}] installing stock-amplifier agent (this takes a few minutes) ..."
        )
        await install_agent(agent, dtu, log_to=out_dir / "install.log")

        if task.workspace_dir.is_dir():
            for child in sorted(task.workspace_dir.iterdir()):
                await dtu.file_push(child, "/workspace/")
        _log(f"[{task.id}] seeded workspace")

        persona = task.meta.get("persona")
        if not persona:
            raise ValueError(f"task {task.id} meta.yaml is missing a 'persona' field")
        invocation_guide = _transport_preamble(dtu.id) + "\n" + agent.invocation_md

        _log(f"[{task.id}] driving the session with the AIUser under test ...")
        result = None
        try:
            result = await asyncio.wait_for(
                ai_user.run(
                    scenario=task.instructions,
                    persona=persona,
                    invocation_guide=invocation_guide,
                ),
                timeout=task.timeout_s,
            )
            (out_dir / "ai_user.json").write_text(
                json.dumps(asdict(result), indent=2, default=str), encoding="utf-8"
            )
            record["verdict"] = result.conclude.verdict if result.conclude else None
            _log(f"[{task.id}] AIUser concluded: verdict={record['verdict']}")
        except asyncio.TimeoutError:
            record["error"] = f"AIUser.run timed out after {task.timeout_s}s"
            _log(f"[{task.id}] AIUser.run TIMED OUT after {task.timeout_s}s")

        # Capture the ACTUAL conversation (the lower agent's tmux scrollback) so the
        # grader can analyze how the AI User drove the session and where it ended,
        # rather than trusting the AI User's self-reported verdict label.
        conversation = ""
        try:
            cap = await dtu.exec_cmd(
                [
                    "bash",
                    "-lc",
                    "tmux capture-pane -p -t agent -S - 2>/dev/null "
                    "|| echo '(no tmux agent session found to capture)'",
                ],
                timeout_s=60,
            )
            conversation = cap.stdout
            (out_dir / "conversation.txt").write_text(conversation, encoding="utf-8")
            _log(f"[{task.id}] captured conversation ({len(conversation)} chars)")
        except Exception as exc:  # noqa: BLE001 - non-fatal; grader still has the self-report
            _log(f"[{task.id}] WARN could not capture conversation: {exc}")

        _log(f"[{task.id}] grading ...")
        task_context = _grader_context(task.instructions, result, conversation)
        grade = await grader.run(
            task.grader_yaml_path,
            task_context,
            dtu.id,
            out_dir / "grader",
            task.grader_data_dir,
        )
        record["grader_score"] = grade.overall_score
        _log(f"[{task.id}] grader overall_score={grade.overall_score}")

        # Outcome is grader-driven. The AI User's verdict label is recorded for
        # reference but does NOT gate pass/fail: a "success" verdict often just
        # means the conversation ended cleanly, not that the user's goal was met.
        threshold = float(task.meta.get("grader_pass_threshold", 1.0))
        record["outcome"] = "PASS" if grade.overall_score >= threshold else "FAIL"
    except Exception as exc:  # noqa: BLE001 - record and continue to next scenario
        record["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"[{task.id}] ERROR: {record['error']}")
    finally:
        _log(f"[{task.id}] destroying DTU {dtu.id} ...")
        await dtu.destroy()

    (out_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


async def _amain(selected: list[str]) -> int:
    if not cli_available():
        _log("ERROR: amplifier-digital-twin CLI not available on PATH")
        return 2

    agent = load_agent(AGENTS_DIR / "stock-amplifier")

    task_dirs = sorted(d for d in TASKS_DIR.iterdir() if (d / "task.yaml").is_file())
    tasks = [load_task(d) for d in task_dirs]
    if selected:
        tasks = [t for t in tasks if any(s in t.id for s in selected)]
    if not tasks:
        _log("No matching scenarios found.")
        return 1

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_base = WORKSPACE_ROOT / ".amplifier" / "evaluation" / "aiuser" / run_id
    out_base.mkdir(parents=True, exist_ok=True)
    _log(f"Run id {run_id}")
    _log(f"Scenarios: {', '.join(t.id for t in tasks)}")
    _log(f"Output: {out_base}")

    foundation_src, provider_src = _resolve_sources()
    _log(f"Foundation source: {foundation_src}")
    _log(f"Provider source:   {provider_src}")

    ai_user = AIUser(foundation_source=foundation_src, provider_source=provider_src)
    grader = Grader(foundation_source=foundation_src, provider_source=provider_src)
    _log("Setting up AIUser + Grader sessions (one-time, expensive) ...")
    await asyncio.gather(ai_user.setup(), grader.setup())
    _log("Setup complete.")

    records = []
    for task in tasks:
        record = await run_scenario(task, agent, ai_user, grader, out_base / task.id)
        records.append(record)

    summary = {"run_id": run_id, "scenarios": records}
    (out_base / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    _log("=" * 60)
    _log("SUMMARY")
    for r in records:
        _log(
            f"  {r['task_id']}: {r['outcome']}  "
            f"(verdict={r['verdict']}, expected={r['expected_verdict']}, "
            f"grader={r['grader_score']})"
            + (f"  error={r['error']}" if r["error"] else "")
        )
    _log(f"Full results: {out_base}")

    return 0 if all(r["outcome"] == "PASS" for r in records) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the standalone AIUser evaluations."
    )
    parser.add_argument(
        "scenarios",
        nargs="*",
        help="Optional scenario id substrings to filter (default: run all).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args.scenarios)))


if __name__ == "__main__":
    main()
