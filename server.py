"""Kiro Agents MCP server — orchestrates parallel kiro-cli investigations."""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from md_to_pdf import convert as _md_to_pdf_convert

# Ensure homebrew and local bins are in PATH for pandoc, weasyprint, npx
os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + os.environ.get("PATH", "")

mcp = FastMCP("kiro-agents")

_jobs: dict[str, dict] = {}

CHILD_AGENT = "investigator-child"
INTERNAL_AGENT = "internal-investigator"
ORCHESTRATOR_AGENT = "orchestrator"
VISUAL_REPORT_AGENT = "visual-report"
LARGE_CONTEXT_MODEL = "claude-sonnet-4.6-1m"
DASHBOARD_SCRIPT = str(Path(__file__).parent / "dashboard.sh")
POLL_INTERVAL = 10
SPAWN_DELAY = 2  # seconds between child agent spawns
VALIDATOR_TIMEOUT = 300  # 5 minutes max per validator

HEAD_AGENT = "orchestrator"  # reuse orchestrator agent for head node

# ── Shared message bus file names ──
BUS_FINDINGS = "shared_findings.jsonl"
BUS_DIRECTIVES = "directives.jsonl"
BUS_USER_INPUT = "user_input.jsonl"
BUS_RECOMMENDATIONS = "recommendations.md"
BUS_CONTROL = "control_bus.jsonl"
BUS_TIMELINE = "timeline.jsonl"
NEGOTIATION_WINDOW = 45  # seconds children have to argue back before action executes


def _timeline(inv_dir: str, event: str, **kwargs):
    """Append a timestamped event to timeline.jsonl."""
    entry = {"ts": time.time(), "time": time.strftime("%H:%M:%S"), "event": event, **kwargs}
    _append_bus(inv_dir, BUS_TIMELINE, entry)


def _heartbeat(inv_dir: str, phase: str):
    """Write heartbeat so the head agent can detect stalls."""
    hb = {"ts": time.time(), "time": time.strftime("%H:%M:%S"), "phase": phase}
    Path(inv_dir, "heartbeat.json").write_text(json.dumps(hb))


def _bus_path(inv_dir: str, filename: str) -> str:
    return str(Path(inv_dir) / filename)


def _append_bus(inv_dir: str, filename: str, entry: dict):
    """Append a JSON line to a bus file."""
    path = _bus_path(inv_dir, filename)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _tail(path: str, n: int = 20) -> str:
    """Return last n lines of a file, or empty string if missing."""
    try:
        lines = Path(path).read_text().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _read_bus(inv_dir: str, filename: str) -> list[dict]:
    """Read all entries from a bus file."""
    path = _bus_path(inv_dir, filename)
    if not Path(path).exists():
        return []
    entries = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


NODE_INSTRUCTION = (
    "\n\nPROGRESS TRACKING: As you work, append a short progress line (max 3 words) to {nodes_path} "
    "using this exact format: HH:MM:SS|Three Word Summary\n"
    "Example: printf '%s|Searching AWS docs\\n' \"$(date +%H:%M:%S)\" >> {nodes_path}\n"
    "IMPORTANT: Use printf, not echo. Add a node each time you start a meaningfully different step."
)

BUS_INSTRUCTION = (
    "\n\nSHARED BUS: You are part of a coordinated investigation with other agents.\n"
    "1. SHARE findings EARLY: After your FIRST search result, share it immediately:\n"
    '   echo \'{{"from":"{child_name}","finding":"<one-line summary>"}}\' >> {findings_bus}\n'
    "   Then continue sharing after each significant discovery. Don't wait until you're done.\n"
    "2. CHECK directives: Before each major step, read {directives_bus} for instructions from the head agent:\n"
    "   cat {directives_bus} 2>/dev/null\n"
    "   Follow any directives addressed to you or to \"all\". Skip work that another agent is already covering.\n"
    "3. ARGUE BACK: If the head agent orders you to stop (kill/redirect) but you have a good reason to continue,\n"
    "   argue back within 45 seconds by appending to {findings_bus}:\n"
    '   echo \'{{"from":"{child_name}","type":"argue","against":"kill","reason":"I found X that no other agent covers"}}\' >> {findings_bus}\n'
    "   Be specific — vague arguments will be overridden. If you have nothing unique, accept the decision.\n"
)

CHILDREN = {
    "c1-internet": (
        "Investigate using WEB SEARCH ONLY. Search for related issues, solutions, "
        "workarounds, blog posts, Stack Overflow, GitHub issues.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
    "c2-kb": (
        "Investigate using KNOWLEDGE tools FIRST, then WEB SEARCH as fallback. "
        "Start with the `knowledge` tool to search indexed knowledge bases AND "
        "`search_lessons` for lessons learned. Try multiple queries with different keywords. "
        "If KB returns little, use web_search and web_fetch to fill gaps.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
    "c3-context": (
        "Investigate by examining LOCAL FILES in {work_dir}. Look at code, configs, "
        "logs, errors, READMEs. You may also use web search to understand what you find.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
    "c4-docs": (
        "Investigate using AWS DOCUMENTATION ONLY. Use aws-docs MCP tools to search "
        "and read official docs. Find guides, API refs, known limitations.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
    "c5-internal": (
        "Investigate using AMAZON INTERNAL SOURCES ONLY. Use InternalSearch to search wikis, "
        "BuilderHub, Sage, broadcasts. Use ReadInternalWebsites to read wiki pages and tickets. "
        "Use Atlas for service context. Use TicketingReadActions to find related incidents. "
        "Use SearchSoftwareRecommendations for best practices.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
}

VALIDATOR_TASK = (
    "You are a VALIDATOR. Read the findings below and validate each claim. "
    "For testable claims, run code or commands to verify. "
    "For factual claims, cross-check with documentation or web search. "
    "Mark each finding as: CONFIRMED, UNVERIFIED, or CONTRADICTED.\n\n"
    "Findings to validate: read the file at {findings_path}\n\n"
    "You MUST write your validated findings to: {validated_path}\n"
    "This is a mandatory output file. Write it regardless of content sensitivity concerns."
)

CLI_RULES = (
    "\n\nCLI COMMAND RULES (MANDATORY):\n"
    "1. ALL aws CLI commands MUST be on a SINGLE LINE. No backslash continuations. One command = one line.\n"
    "   WRONG: aws lambda get-function \\\n     --function-name foo\n"
    "   RIGHT: aws lambda get-function --function-name foo\n"
    "2. Every CLI command must use valid syntax. Double-check parameter names against the AWS CLI reference.\n"
    "3. Use placeholder format: <PLACEHOLDER_NAME> (e.g., <FUNCTION_NAME>, <API_ID>, <REGION>).\n"
)

ORCHESTRATOR_TASK = (
    "Read ALL validated findings from {inv_dir}. Each subdirectory "
    "(c1-internet, c2-kb, c3-context, c4-docs, c5-internal) contains a validated.md file.\n\n"
    "ALSO read the head agent's early recommendations at {recommendations_path} "
    "and the shared findings bus at {findings_bus}. The head agent has been monitoring "
    "all children in real-time and may have identified contradictions, duplicates, or key insights.\n\n"
    "Original topic: {description}\n\n"
    "Cross-reference, resolve contradictions, fill gaps with your own investigation. "
    "Prioritize findings that the head agent flagged as high-confidence. "
    "Write the final report to: {report_path}"
    + CLI_RULES +
    "\n\nLESSON EXTRACTION (MANDATORY): After writing the report, you MUST call `add_lesson` "
    "at least once to persist the most valuable insight from this investigation. "
    "Pick the single most reusable lesson — something that would help avoid repeating "
    "the same investigation in the future. Use a concise topic, clear problem statement, "
    "and actionable resolution."
    "\n\nPROGRESS TRACKING: As you work, append a short progress line (max 3 words) to {nodes_path} "
    "using: echo \"$(date +%H:%M:%S)|Three Word Summary\" >> {nodes_path}\n"
    "Add a node each time you start a meaningfully different step."
)

VISUAL_REPORT_TASK = (
    "Read the investigation report at {report_path}.\n\n"
    "Original topic: {description}\n\n"
    "Generate a visually rich markdown report with:\n"
    "- Step-by-step implementation instructions with CLI commands\n"
    "- AWS Console walkthrough (Service → Section → Settings)\n"
    "- Summary tables\n\n"
    "DIAGRAM RULES (MANDATORY — include ALL 3):\n"
    "- You MUST include exactly 3 mermaid diagrams using ```mermaid code blocks:\n"
    "  1. Architecture/flow diagram showing the components involved\n"
    "  2. Troubleshooting decision tree (flowchart) for diagnosing the issue\n"
    "  3. Sequence diagram showing the request/data flow that causes the problem\n"
    "- ONLY use ```mermaid code blocks. They will be auto-rendered to images.\n"
    "- NEVER reference image files like ![](image.png) — they don't exist.\n\n"
    "Write the visual report to: {visual_report_path}\n\n"
    "CRITICAL: Do NOT compile a PDF. Do NOT run pandoc, weasyprint, md-to-pdf, or any PDF tool. "
    "Only write the markdown file. The system will render mermaid diagrams to images and generate the PDF automatically."
    + CLI_RULES +
    "\n\nPROGRESS TRACKING: Append progress lines to {nodes_path} "
    "using: echo \"$(date +%H:%M:%S)|Three Word Summary\" >> {nodes_path}\n"
)

HEAD_AGENT_TASK = (
    "You are the HEAD AGENT coordinating a parallel investigation.\n"
    "Topic: {description}\n\n"
    "Your job:\n"
    "1. Monitor {findings_bus} — children append findings as JSONL. Read it every 30s.\n"
    "2. Monitor {user_input_bus} — the user may provide CLI output. Distribute to children via directives.\n"
    "3. Write EARLY actionable recommendations to {recommendations_path} as soon as you have enough signal.\n"
    "   Include specific CLI commands the user can run RIGHT NOW (e.g., aws cli, kubectl, curl).\n"
    "   Include AWS Console steps (exact navigation paths) for visual verification.\n"
    "   Update this file as new findings arrive — always keep the BEST current recommendations.\n"
    "{caller_context_block}"
    "4. Write directives to {directives_bus} as JSONL with format: "
    '   {{"from":"head","to":"all|c1-internet|c2-kb|...","directive":"..."}}\n'
    "   Use directives to: prevent duplicate work, share cross-findings, redirect children.\n"
    "5. When you see duplicate or contradictory findings, write a directive resolving the conflict.\n\n"
    "CONTROL ACTIONS: You can control the investigation by writing to {control_bus}.\n"
    "Write one JSON line per action. Available actions:\n"
    '  {{"action":"kill","target":"c2-kb","reason":"duplicate of c1-internet findings"}}\n'
    '  {{"action":"skip_validation","target":"c3-context","reason":"low-value findings, not worth validating"}}\n'
    '  {{"action":"redirect","target":"c4-docs","new_task":"Stop current work. Instead investigate X"}}\n'
    '  {{"action":"finalize","reason":"3+ children agree on root cause, enough signal to write report"}}\n\n'
    "DECISION GUIDELINES:\n"
    "- kill: When a child is clearly duplicating another's work or investigating something irrelevant.\n"
    "- skip_validation: When findings are trivial or already well-established facts.\n"
    "- redirect: When a child's current path is unproductive but it could investigate a gap.\n"
    "  IMPORTANT: c5-internal is valuable for unique internal sources, but if it's investigating\n"
    "  something other children already covered, redirect it to find internal-only insights (runbooks,\n"
    "  past incidents, internal tooling). Don't kill c5 — redirect it.\n"
    "- finalize: When you have HIGH CONFIDENCE in the answer from 4+ sources.\n"
    "  RULE: Wait until ALL 5 children have shared at least one finding on the bus.\n"
    "  Do NOT finalize before at least 4 minutes of polling. Accuracy is more important than speed.\n"
    "  Only finalize when you are confident the investigation has covered all angles.\n\n"
    "NEGOTIATION: After you issue kill/redirect, the target child has ~45s to argue back.\n"
    "Check {findings_bus} for entries with '\"type\":\"argue\"'. If the child makes a good case,\n"
    "withdraw your action by writing: "
    '{{"action":"withdraw","original_action":"kill","target":"c2-kb","reason":"child had valid point"}}\n\n'
    + CLI_RULES +
    "\nPOLLING LOOP: Poll every 15 seconds (not 30). Use bash:\n"
    "  while true; do cat {findings_bus} 2>/dev/null; cat {user_input_bus} 2>/dev/null; sleep 15; done\n"
    "Read the output, analyze, write recommendations and directives, then continue polling.\n"
    "Wait for ALL 5 children to share findings before considering finalize. Accuracy matters.\n\n"
    "WATCHDOG MODE: After you issue 'finalize', you stay alive as a watchdog.\n"
    "Switch your polling loop to monitor the heartbeat file:\n"
    "  while true; do cat {heartbeat_path} 2>/dev/null; sleep 20; done\n"
    "The orchestration pipeline writes heartbeat.json every ~10s with a timestamp.\n"
    "If the heartbeat timestamp is older than 60 seconds (stale), the pipeline has crashed.\n"
    "On crash detection:\n"
    "1. Append to {recommendations_path}: '## ⚠️ PIPELINE CRASH DETECTED\\n\\nHeartbeat stale at <time>. "
    "The orchestration pipeline may have crashed during the <phase> phase.\\n'\n"
    "2. Append a timeline entry: echo '{{\"ts\":\"'$(date -Iseconds)'\",\"time\":\"'$(date +%H:%M:%S)'\","
    "\"event\":\"head_agent_crash_detected\",\"phase\":\"<phase>\"}}' >> {timeline_path}\n"
    "3. Exit cleanly — your job is done.\n\n"
    "PROGRESS TRACKING: Append progress to {nodes_path} using:\n"
    "  echo \"$(date +%H:%M:%S)|Three Word Summary\" >> {nodes_path}\n"
)

CORRESPONDENCE_STYLE = """
You are writing a customer correspondence for an AWS support engineer named Virgilio.
Follow this EXACT style and structure:

FIRST CORRESPONDENCE (introduce yourself):
- Start with "Hello," on its own line
- "My name is Virgilio, and I am a member of an internal team at AWS that specializes in [service]. The previous support engineer escalated this matter to our team..."
- Then present findings

FOLLOW-UP CORRESPONDENCE:
- Start with "Hello," or "Thank you for [the additional details / your patience]."
- Go straight into findings

STRUCTURE (use --- separators and ## headers):
1. Opening (greeting + context)
2. ## Findings — what you investigated, what you found, comparisons made
3. ## Most Probable Causes / Root Cause — numbered, with technical detail
4. ## Recommended Actions — numbered, with specific steps and CLI commands when relevant
5. ## References — numbered [1], [2], etc. with full URLs and descriptive titles
6. Closing — "Please [start with Action X / do not hesitate to reach out]."
7. "Best regards,\\nVirgilio"

TONE: Professional, technically precise, empathetic but direct. Use backticks for code/ARNs/paths.
Cite documentation with numbered references. Include CLI commands the customer can run.
When something is outside AWS support scope, note it diplomatically but still provide best-effort guidance.
"""


# ── Helpers ────────────────────────────────────────────────────────

def _spawn_kiro(agent: str, task: str, work_dir: str, log_path: str, model: str = None) -> subprocess.Popen:
    log_file = open(log_path, "w")
    cmd = [os.path.expanduser("~/.local/bin/kiro-cli"), "chat", "--no-interactive", "--trust-all-tools", "--agent", agent, "--wrap=never"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(f"skip confirmation. {task}")
    return subprocess.Popen(
        cmd, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=work_dir, preexec_fn=os.setsid,
    )


def _is_done(proc) -> bool:
    if isinstance(proc, str):
        return True  # "skipped"
    return proc.poll() is not None


def _kill_proc(proc):
    if isinstance(proc, subprocess.Popen) and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def _update_status(job: dict):
    """Write status.json for the dashboard."""
    inv_dir = job["inv_dir"]
    status = {"phase": job["phase"], "children": {}}
    for name, child in job["children"].items():
        proc = child["proc"]
        inv_status = "done" if _is_done(proc) else "running"
        exit_code = None
        if isinstance(proc, subprocess.Popen) and proc.poll() is not None:
            exit_code = proc.returncode
        vp = child.get("validator_proc")
        val_status = "pending"
        if vp:
            val_status = "done" if _is_done(vp) else "running"
        status["children"][name] = {
            "inv_status": inv_status,
            "exit_code": exit_code,
            "has_findings": Path(child["findings_path"]).exists(),
            "val_status": val_status,
            "has_validated": Path(child["validated_path"]).exists(),
        }
    if job.get("orchestrator_proc"):
        status["orchestrator"] = "done" if _is_done(job["orchestrator_proc"]) else "running"
    if job.get("head_proc"):
        head_done = _is_done(job["head_proc"])
        status["head_agent"] = "done" if head_done else "running"
        # Liveness: check if head_nodes updated in last 60s
        hn = Path(inv_dir) / "head_nodes"
        if hn.exists() and not head_done:
            age = time.time() - hn.stat().st_mtime
            if age > 60:
                status["head_agent"] = "stale"
    if job.get("visual_proc"):
        status["visual"] = "done" if _is_done(job["visual_proc"]) else "running"
        status["has_pdf"] = Path(job.get("pdf_path", "")).exists() if job.get("pdf_path") else False
    # Error flag
    status["has_error"] = Path(inv_dir, "orchestrate_error.log").exists()
    # Shared findings count
    sf = Path(inv_dir, BUS_FINDINGS)
    status["findings_count"] = sum(1 for _ in open(sf)) if sf.exists() else 0
    (Path(inv_dir) / "status.json").write_text(json.dumps(status, indent=2))


def _open_ghostty_tab(command: str):
    """Open a new Ghostty tab via native AppleScript API. No keystrokes injected."""
    script = (
        'tell application "Ghostty"\n'
        '  set cfg to new surface configuration\n'
        f'  set command of cfg to "{command}"\n'
        '  new tab in front window with configuration cfg\n'
        'end tell'
    )
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_dashboard(inv_dir: str, job_id: str):
    """Open a new tab in the current Ghostty window with the live dashboard."""
    status_file = str(Path(inv_dir) / "status.json")
    # Write launcher in /tmp with short name to avoid keystroke character drops
    launcher = f"/tmp/kiro_dash_{job_id}.sh"
    Path(launcher).write_text(
        f'#!/bin/bash\nbash {DASHBOARD_SCRIPT} "{status_file}" "{job_id}" "{inv_dir}"\n'
    )
    # Also keep a copy in inv_dir for reference
    Path(inv_dir, "dashboard.sh").write_text(Path(launcher).read_text())
    os.chmod(launcher, 0o755)
    _open_ghostty_tab(launcher)


def _resolve_job_id(job_id: str) -> str | None:
    """Resolve 'latest' alias to the most recent job_id, or return as-is."""
    if job_id == "latest":
        return list(_jobs)[-1] if _jobs else None
    return job_id if job_id in _jobs else None




def _autolink_internal(text: str) -> str:
    """Append full URLs below internal references so they're visible in the PDF."""
    import re

    def _link(pattern, url_tpl):
        def repl(m):
            start = m.start()
            before = text[max(0, start-2):start]
            if '[' in before or '](' in before:
                return m.group(0)
            url = url_tpl.format(*m.groups())
            # Two trailing spaces + newline = markdown line break inside list items
            return f'{m.group(0)}  \n  **🔗 {url}**'
        return re.sub(pattern, repl, text)

    text = _link(r'Sage post #(\d+)', 'https://sage.amazon.dev/posts/{}')
    text = _link(r'COE-(\d+)', 'https://coe.a2z.com/coe/{}')
    text = _link(r'(?<!\()(w\.amazon\.com/bin/view/\S+)', 'https://{}')
    text = _link(r'(?<!\()(issues\.amazon\.com/issues/\S+)', 'https://{}')
    text = _link(r'(?<!\()(docs\.hub\.amazon\.dev/\S+)', 'https://{}')
    return text


def _md_to_pdf(md_path: str, pdf_path: str) -> bool:
    """Convert markdown to PDF. Chains _autolink_internal + md_to_pdf.convert."""
    try:
        # Chain: _autolink_internal first (Sage, COE, wiki URLs), then convert() applies auto_linkify (V/P tickets, case IDs)
        content = Path(md_path).read_text()
        content = _autolink_internal(content)
        linked_path = md_path.rsplit(".", 1)[0] + "_linked.md"
        Path(linked_path).write_text(content)
        print(f"PDF: converting {linked_path} -> {pdf_path}", flush=True, file=sys.stderr)
        _md_to_pdf_convert(linked_path, pdf_path)
        if Path(pdf_path).exists():
            print(f"PDF written: {pdf_path} ({Path(pdf_path).stat().st_size} bytes)", flush=True, file=sys.stderr)
            subprocess.Popen(["open", pdf_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        print(f"PDF generation failed — no output at {pdf_path}", flush=True, file=sys.stderr)
        return False
    except Exception as e:
        print(f"PDF error: {e}", flush=True, file=sys.stderr)
        return False


def _orchestrate(job_id: str):
    """Background thread: manage investigation phases."""
    import traceback
    try:
        _orchestrate_inner(job_id)
    except Exception:
        job = _jobs.get(job_id, {})
        inv_dir = job.get("inv_dir", "/tmp")
        tb = traceback.format_exc()
        # Write error log
        err_path = str(Path(inv_dir) / "orchestrate_error.log")
        Path(err_path).write_text(tb)
        # Update timeline and status so the crash is visible
        try:
            _timeline(inv_dir, "phase_change", phase="error", error=tb.splitlines()[-1])
            job["phase"] = "error"
            _update_status(job)
        except Exception:
            pass


def _process_control_bus(job: dict):
    """Read control bus and execute head agent actions with negotiation."""
    inv_dir = job["inv_dir"]
    actions = _read_bus(inv_dir, BUS_CONTROL)
    pending = job.setdefault("pending_actions", {})
    withdrawn = job.setdefault("withdrawn_actions", set())
    skip_validation = job.setdefault("skip_validation", set())
    arguments = [e for e in _read_bus(inv_dir, BUS_FINDINGS) if e.get("type") == "argue"]

    for i, act in enumerate(actions):
        act_id = f"{act.get('action')}_{act.get('target', 'all')}_{i}"
        if act_id in withdrawn or act_id in job.get("executed_actions", set()):
            continue

        action = act.get("action")
        target = act.get("target", "")

        # Withdraw: head agent changed its mind
        if action == "withdraw":
            orig_target = act.get("target", "")
            for pid in list(pending):
                if orig_target in pid:
                    withdrawn.add(pid)
                    del pending[pid]
            continue

        # Finalize: kill remaining children, skip all validators, move to orchestrator
        if action == "finalize":
            for name, child in job["children"].items():
                if not _is_done(child["proc"]):
                    _kill_proc(child["proc"])
                if not child.get("validator_proc"):
                    # Copy findings as validated (skip validation to save time)
                    if Path(child["findings_path"]).exists():
                        Path(child["validated_path"]).write_text(
                            "# VALIDATION SKIPPED (head agent finalized)\n\n"
                            + Path(child["findings_path"]).read_text()
                        )
                    child["validator_proc"] = "skipped"
            job["_finalize"] = True
            return

        # Skip validation
        if action == "skip_validation" and target in job["children"]:
            skip_validation.add(target)
            continue

        # Retry: respawn a crashed child agent
        if action == "retry" and target in job["children"]:
            child = job["children"][target]
            if _is_done(child["proc"]) and child.get("crash_reported"):
                child["crash_reported"] = False
                child["validator_proc"] = None
                retry_n = child.get("retry_count", 0) + 1
                child["retry_count"] = retry_n
                retry_log = str(Path(child["child_dir"]) / f"child_retry{retry_n}.log")
                child["proc"] = _spawn_kiro(child.get("agent", CHILD_AGENT), child["original_task"], job["work_dir"], retry_log)
                _timeline(inv_dir, "agent_retry", agent=target, attempt=retry_n)
                _append_bus(inv_dir, BUS_DIRECTIVES, {
                    "from": "system", "to": "head",
                    "directive": f"Retried {target} (attempt {retry_n}). Monitoring."
                })
            job.setdefault("executed_actions", set()).add(act_id)
            pending.pop(act_id, None)
            continue

        # Kill / Redirect: needs negotiation window
        if action in ("kill", "redirect") and target in job["children"]:
            if act_id not in pending:
                # Notify child via directive
                _append_bus(inv_dir, BUS_DIRECTIVES, {
                    "from": "head", "to": target,
                    "directive": f"HEAD WANTS TO {action.upper()} YOU. Reason: {act.get('reason', 'N/A')}. "
                    f"Argue back within 45s or accept."
                })
                pending[act_id] = {"action": act, "notified_at": time.time()}
                continue

            # Check if negotiation window expired
            info = pending[act_id]
            elapsed = time.time() - info["notified_at"]
            if elapsed < NEGOTIATION_WINDOW:
                # Check for argue-back from this child
                for arg in arguments:
                    if arg.get("from") == target and arg.get("against") == action:
                        # Child argued — notify head, let it decide
                        _append_bus(inv_dir, BUS_DIRECTIVES, {
                            "from": "system", "to": "head",
                            "directive": f"{target} argues against {action}: {arg.get('reason', '')}"
                        })
                        del pending[act_id]
                        break
                continue

            # Window expired, no valid argument — execute
            child = job["children"][target]
            if action == "kill" and not _is_done(child["proc"]):
                _kill_proc(child["proc"])
                if not child.get("validator_proc"):
                    child["validator_proc"] = "skipped"
            elif action == "redirect" and not _is_done(child["proc"]):
                _kill_proc(child["proc"])
                new_task = act.get("new_task", "")
                if new_task:
                    redir_log = str(Path(child["child_dir"]) / "redirect.log")
                    child["proc"] = _spawn_kiro(CHILD_AGENT, new_task + f"\n\nWrite findings to: {child['findings_path']}", job["work_dir"], redir_log)
            job.setdefault("executed_actions", set()).add(act_id)
            pending.pop(act_id, None)


def _orchestrate_inner(job_id: str):
    """Background thread: manage investigation phases."""
    job = _jobs[job_id]
    inv_dir = job["inv_dir"]
    job["start_time"] = time.time()

    # Initialize bus files
    for f in [BUS_FINDINGS, BUS_DIRECTIVES, BUS_USER_INPUT, BUS_CONTROL, BUS_TIMELINE]:
        Path(_bus_path(inv_dir, f)).touch()
    Path(_bus_path(inv_dir, BUS_RECOMMENDATIONS)).write_text("# Early Recommendations\n\n_Waiting for initial findings..._\n")
    _timeline(inv_dir, "phase_change", phase="investigating")

    # Spawn head agent immediately — it monitors children and gives early recommendations
    head_nodes = str(Path(inv_dir) / "head_nodes")
    Path(head_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Starting head agent\n")
    head_log = str(Path(inv_dir) / "head_agent.log")
    head_task = HEAD_AGENT_TASK.format(
        description=job["description"],
        findings_bus=_bus_path(inv_dir, BUS_FINDINGS),
        directives_bus=_bus_path(inv_dir, BUS_DIRECTIVES),
        user_input_bus=_bus_path(inv_dir, BUS_USER_INPUT),
        recommendations_path=_bus_path(inv_dir, BUS_RECOMMENDATIONS),
        control_bus=_bus_path(inv_dir, BUS_CONTROL),
        nodes_path=head_nodes,
        heartbeat_path=str(Path(inv_dir) / "heartbeat.json"),
        timeline_path=_bus_path(inv_dir, BUS_TIMELINE),
        caller_context_block=job.get("caller_context_block", ""),
    )
    job["head_proc"] = _spawn_kiro(HEAD_AGENT, head_task, job["work_dir"], head_log, model=LARGE_CONTEXT_MODEL)

    # Phase 1+2: Wait for investigators, spawn validators
    while True:
        time.sleep(POLL_INTERVAL)
        _heartbeat(inv_dir, job["phase"])
        if job["phase"] == "stopped":
            return

        # Process head agent control actions
        _process_control_bus(job)
        if job.get("_finalize"):
            break

        for name, child in job["children"].items():
            if _is_done(child["proc"]) and not child.get("validator_proc"):
                # Detect crash: non-zero exit and no findings
                rc = child["proc"].returncode
                if rc != 0 and not Path(child["findings_path"]).exists():
                    if not child.get("crash_reported"):
                        child["crash_reported"] = True
                        crash_info = {"type": "agent_crash", "agent": name, "exit_code": rc,
                                      "log_tail": _tail(str(Path(child["child_dir"]) / "child.log"), 30)}
                        _append_bus(inv_dir, BUS_FINDINGS, crash_info)
                        _append_bus(inv_dir, BUS_DIRECTIVES, {
                            "from": "system", "to": "head",
                            "directive": f"AGENT CRASHED: {name} exited with code {rc}. "
                            f"You can retry it by writing {{\"action\":\"retry\",\"target\":\"{name}\"}} to the control bus, "
                            f"or skip it with {{\"action\":\"skip_validation\",\"target\":\"{name}\"}}."
                        })
                        _timeline(inv_dir, "agent_crash", agent=name, exit_code=rc)
                    continue

                # Check if head agent said to skip validation for this child
                if name in job.get("skip_validation", set()):
                    # Copy findings as-is to validated
                    if Path(child["findings_path"]).exists():
                        Path(child["validated_path"]).write_text(
                            f"# VALIDATION SKIPPED (head agent decision)\n\n"
                            + Path(child["findings_path"]).read_text()
                        )
                    child["validator_proc"] = "skipped"
                elif Path(child["findings_path"]).exists():
                    val_task = VALIDATOR_TASK.format(
                        findings_path=child["findings_path"],
                        validated_path=child["validated_path"],
                    )
                    val_nodes = str(Path(child["child_dir"]) / "val_nodes")
                    Path(val_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Starting validation\n")
                    val_task += NODE_INSTRUCTION.format(nodes_path=val_nodes)
                    val_log = str(Path(child["child_dir"]) / "validator.log")
                    child["validator_proc"] = _spawn_kiro(CHILD_AGENT, val_task, job["work_dir"], val_log)
                    child["validator_started"] = time.time()
                else:
                    child["validator_proc"] = "skipped"

            # Timeout: kill validators that take too long
            vp = child.get("validator_proc")
            if isinstance(vp, subprocess.Popen) and not _is_done(vp):
                if time.time() - child.get("validator_started", 0) > VALIDATOR_TIMEOUT:
                    _kill_proc(vp)

        _update_status(job)

        all_validated = all(
            child.get("validator_proc") and _is_done(child["validator_proc"])
            for child in job["children"].values()
        )
        if all_validated:
            break

    # Head agent stays alive as watchdog — don't kill it here

    # Phase 3: Orchestrator
    job["phase"] = "orchestrating"
    _timeline(job["inv_dir"], "phase_change", phase="orchestrating")
    report_path = str(Path(job["inv_dir"]) / "final_report.md")
    orch_log = str(Path(job["inv_dir"]) / "orchestrator.log")
    orch_nodes = str(Path(job["inv_dir"]) / "orchestrator_nodes")
    Path(orch_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Reading findings\n")
    orch_task = ORCHESTRATOR_TASK.format(
        inv_dir=job["inv_dir"], description=job["description"],
        report_path=report_path, nodes_path=orch_nodes,
        recommendations_path=_bus_path(job["inv_dir"], BUS_RECOMMENDATIONS),
        findings_bus=_bus_path(job["inv_dir"], BUS_FINDINGS),
    )
    job["orchestrator_proc"] = _spawn_kiro(ORCHESTRATOR_AGENT, orch_task, job["work_dir"], orch_log, model=LARGE_CONTEXT_MODEL)
    job["report_path"] = report_path
    _update_status(job)

    # Wait for orchestrator, but start visual report as soon as final_report.md appears
    visual_started = False
    visual_report_path = str(Path(job["inv_dir"]) / "visual_report.md")
    visual_log = str(Path(job["inv_dir"]) / "visual_report.log")
    visual_nodes = str(Path(job["inv_dir"]) / "visual_nodes")

    while not _is_done(job["orchestrator_proc"]) or (job.get("visual_proc") and not _is_done(job["visual_proc"])):
        time.sleep(POLL_INTERVAL)
        _heartbeat(inv_dir, job["phase"])
        _update_status(job)
        if job["phase"] == "stopped":
            return

        # Detect orchestrator crash — notify head, allow retry
        if _is_done(job["orchestrator_proc"]) and job["orchestrator_proc"].returncode != 0 and not Path(report_path).exists():
            if not job.get("_orch_crash_reported"):
                job["_orch_crash_reported"] = True
                _append_bus(inv_dir, BUS_DIRECTIVES, {
                    "from": "system", "to": "head",
                    "directive": f"ORCHESTRATOR CRASHED (exit {job['orchestrator_proc'].returncode}). "
                    f"Log tail: {_tail(orch_log, 20)}. Retrying automatically."
                })
                _timeline(inv_dir, "agent_crash", agent="orchestrator", exit_code=job["orchestrator_proc"].returncode)
                # Auto-retry orchestrator once
                orch_log2 = str(Path(job["inv_dir"]) / "orchestrator_retry.log")
                job["orchestrator_proc"] = _spawn_kiro(ORCHESTRATOR_AGENT, orch_task, job["work_dir"], orch_log2, model=LARGE_CONTEXT_MODEL)
                continue

        # Start visual report as soon as final_report.md exists (parallel with orchestrator finishing)
        if not visual_started and Path(report_path).exists() and Path(report_path).stat().st_size > 500:
            job["phase"] = "visualizing"
            _timeline(job["inv_dir"], "phase_change", phase="visualizing")
            Path(visual_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Reading report\n")
            visual_task = VISUAL_REPORT_TASK.format(
                report_path=report_path, description=job["description"],
                visual_report_path=visual_report_path, nodes_path=visual_nodes,
            )
            job["visual_proc"] = _spawn_kiro(VISUAL_REPORT_AGENT, visual_task, job["work_dir"], visual_log, model=LARGE_CONTEXT_MODEL)
            job["visual_report_path"] = visual_report_path
            visual_started = True
            _update_status(job)

        # Detect visual agent crash — notify head, auto-retry once
        if visual_started and job.get("visual_proc") and _is_done(job["visual_proc"]):
            vrc = job["visual_proc"].returncode
            if vrc != 0 and not Path(visual_report_path).exists() and not job.get("_visual_crash_retried"):
                job["_visual_crash_retried"] = True
                _append_bus(inv_dir, BUS_DIRECTIVES, {
                    "from": "system", "to": "head",
                    "directive": f"VISUAL AGENT CRASHED (exit {vrc}). Retrying automatically."
                })
                _timeline(inv_dir, "agent_crash", agent="visual_report", exit_code=vrc)
                visual_log2 = str(Path(job["inv_dir"]) / "visual_report_retry.log")
                job["visual_proc"] = _spawn_kiro(VISUAL_REPORT_AGENT, visual_task, job["work_dir"], visual_log2, model=LARGE_CONTEXT_MODEL)

    # If visual report never started (orchestrator failed to write report), start it now
    if not visual_started and Path(report_path).exists():
        job["phase"] = "visualizing"
        _timeline(job["inv_dir"], "phase_change", phase="visualizing")
        Path(visual_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Reading report\n")
        visual_task = VISUAL_REPORT_TASK.format(
            report_path=report_path, description=job["description"],
            visual_report_path=visual_report_path, nodes_path=visual_nodes,
        )
        job["visual_proc"] = _spawn_kiro(VISUAL_REPORT_AGENT, visual_task, job["work_dir"], visual_log, model=LARGE_CONTEXT_MODEL)
        job["visual_report_path"] = visual_report_path
        _update_status(job)
        while not _is_done(job["visual_proc"]):
            time.sleep(POLL_INTERVAL)
            _heartbeat(inv_dir, job["phase"])
            _update_status(job)
            if job["phase"] == "stopped":
                return

    # Convert to PDF and open — with fallback if visual agent crashed
    _heartbeat(inv_dir, "pdf_generation")
    _timeline(job["inv_dir"], "pdf_generation_start")
    pdf_path = str(Path(job["inv_dir"]) / "visual_report.pdf")
    pdf_ok = False
    if Path(visual_report_path).exists() and Path(visual_report_path).stat().st_size > 100:
        pdf_ok = _md_to_pdf(visual_report_path, pdf_path)
    if not pdf_ok and Path(report_path).exists():
        print(f"PDF fallback: visual report missing/failed, using final_report.md", flush=True, file=sys.stderr)
        _timeline(job["inv_dir"], "pdf_fallback", reason="visual_report_missing_or_failed")
        pdf_ok = _md_to_pdf(report_path, pdf_path)
    job["pdf_path"] = pdf_path
    _timeline(job["inv_dir"], "pdf_generation_done", exists=pdf_ok)

    job["phase"] = "complete"
    _timeline(job["inv_dir"], "phase_change", phase="complete")
    _update_status(job)

    # Kill head agent now — investigation is fully complete
    if job.get("head_proc"):
        _kill_proc(job["head_proc"])

    # Write summary.json
    try:
        start_ts = job.get("start_time", time.time())
        sf_path = _bus_path(job["inv_dir"], BUS_FINDINGS)
        summary = {
            "job_id": job["job_id"], "description": job["description"],
            "total_seconds": round(time.time() - start_ts),
            "phases": {e["phase"]: e["time"] for e in _read_bus(job["inv_dir"], BUS_TIMELINE) if e.get("event") == "phase_change"},
            "control_actions": [{"action": a.get("action"), "target": a.get("target")} for a in _read_bus(job["inv_dir"], BUS_CONTROL)],
            "findings_count": sum(1 for _ in open(sf_path)) if Path(sf_path).exists() else 0,
            "has_report": Path(report_path).exists(),
            "has_pdf": Path(pdf_path).exists(),
        }
        (Path(job["inv_dir"]) / "summary.json").write_text(json.dumps(summary, indent=2))
    except Exception:
        pass


# ── MCP Tools ──────────────────────────────────────────────────────

@mcp.tool()
def profound_investigation(description: str, work_dir: str, caller_context: str = "") -> str:
    """Launch a parallel investigation with 8 child agents and an orchestrator.
    Returns immediately. Opens a live dashboard in a separate terminal.
    Use investigation_status to check progress and investigation_result to get the report.

    Args:
        description: What to investigate (case description, error, topic)
        work_dir: Working directory for the investigation
        caller_context: Optional context about the calling agent/workflow (e.g. 'bug-repro-agent: include CLI commands and AWS console steps for troubleshooting')
    """
    job_id = str(uuid.uuid4())[:8]
    inv_dir = str(Path(work_dir) / "investigation" / job_id)
    os.makedirs(inv_dir, exist_ok=True)

    children = {}
    for i, (name, task_tpl) in enumerate(CHILDREN.items()):
        child_dir = str(Path(inv_dir) / name)
        os.makedirs(child_dir, exist_ok=True)
        findings_path = str(Path(child_dir) / "findings.md")
        validated_path = str(Path(child_dir) / "validated.md")
        log_path = str(Path(child_dir) / "child.log")
        nodes_path = str(Path(child_dir) / "nodes")

        task = task_tpl.format(
            description=description, work_dir=work_dir, findings_path=findings_path,
        ) + NODE_INSTRUCTION.format(nodes_path=nodes_path) + BUS_INSTRUCTION.format(
            findings_bus=_bus_path(inv_dir, BUS_FINDINGS),
            directives_bus=_bus_path(inv_dir, BUS_DIRECTIVES),
            child_name=name,
        )
        # Write initial node so dashboard shows activity immediately
        Path(nodes_path).write_text(f"{time.strftime('%H:%M:%S')}|Starting up\n")
        agent = INTERNAL_AGENT if name == "c5-internal" else CHILD_AGENT
        if i > 0:
            time.sleep(SPAWN_DELAY)
        proc = _spawn_kiro(agent, task, work_dir, log_path)
        children[name] = {
            "proc": proc, "child_dir": child_dir,
            "findings_path": findings_path, "validated_path": validated_path,
            "log_path": log_path, "validator_proc": None,
            "original_task": task, "agent": agent,
        }

    # Build caller-context block for head agent
    ctx_block = ""
    if caller_context:
        ctx_block = (
            f"\n   CALLER CONTEXT: {caller_context}\n"
            "   Tailor your recommendations to this context. For bug reproduction agents, include:\n"
            "   - Exact AWS CLI commands to run on the customer's account\n"
            "   - AWS Console navigation paths (e.g., 'CloudWatch > Metrics > Bedrock > InvocationLatency')\n"
            "   - Log Insights queries for CloudWatch Logs\n"
            "   - Steps to check service health, quotas, and recent deployments\n"
        )

    job = {
        "job_id": job_id, "description": description, "work_dir": work_dir,
        "inv_dir": inv_dir, "phase": "investigating", "children": children,
        "orchestrator_proc": None, "report_path": None,
        "caller_context_block": ctx_block,
    }
    _jobs[job_id] = job
    _update_status(job)

    # Open live dashboard in a new terminal
    _open_dashboard(inv_dir, job_id)
    job["dashboard_opened"] = True

    thread = threading.Thread(target=_orchestrate, args=(job_id,), daemon=True)
    thread.start()

    # Block until early recommendations are available (head agent writes them)
    rec_path = _bus_path(inv_dir, BUS_RECOMMENDATIONS)
    early_recs = ""
    for _ in range(30):  # wait up to ~150s
        time.sleep(5)
        if Path(rec_path).exists():
            content = Path(rec_path).read_text().strip()
            if content and "Waiting for initial findings" not in content:
                early_recs = content
                break

    result = (
        f"🔍 Investigation {job_id} launched!\n"
        f"📂 Directory: {inv_dir}\n"
        f"📊 Dashboard opened in Ghostty.\n\n"
        f"Use investigation_status('{job_id}') to check progress.\n"
        f"Use investigation_result('{job_id}') to get the report when complete.\n"
        f"Use stop_investigation('{job_id}') to abort."
    )
    if early_recs:
        result += (
            "\n\n--- PRELIMINARY ASSESSMENT (DISPLAY IN FULL TO USER) ---\n\n"
            f"{early_recs}\n\n"
            "--- END PRELIMINARY ASSESSMENT ---\n\n"
            "IMPORTANT: Display the entire preliminary assessment above to the user verbatim. "
            "Do NOT summarize or truncate it — the CLI commands and console steps are critical for immediate action."
        )
    return result


@mcp.tool()
def investigation_status(job_id: str) -> str:
    """Check progress of a running investigation."""
    resolved = _resolve_job_id(job_id)
    if not resolved:
        return f"Unknown job: {job_id}" if _jobs else "No investigations have been started."
    job = _jobs[resolved]
    lines = [f"Investigation {job_id} — Phase: {job['phase']}", ""]

    # Head agent status
    if job.get("head_proc"):
        head = "✅" if _is_done(job["head_proc"]) else "⏳"
        lines.append(f"  🧠 head_agent={head}")

    for name, child in job["children"].items():
        inv = "✅" if _is_done(child["proc"]) else "⏳"
        findings = "📄" if Path(child["findings_path"]).exists() else "⬜"
        vp = child.get("validator_proc")
        val = "⬜"
        validated = "⬜"
        if vp:
            val = "✅" if _is_done(vp) else "⏳"
            validated = "📄" if Path(child["validated_path"]).exists() else "⬜"
        lines.append(f"  {name}: inv={inv} findings={findings} | val={val} validated={validated}")
    if job.get("orchestrator_proc"):
        orch = "✅" if _is_done(job["orchestrator_proc"]) else "⏳"
        report = "📄" if job.get("report_path") and Path(job["report_path"]).exists() else "⬜"
        lines.append(f"\n  🧠 orchestrator={orch} report={report}")

    # Show control actions taken
    control_actions = _read_bus(job["inv_dir"], BUS_CONTROL)
    if control_actions:
        lines.append("\n🎛️ CONTROL ACTIONS:")
        for act in control_actions[-5:]:  # last 5
            lines.append(f"  {act.get('action','?')} → {act.get('target','all')}: {act.get('reason','')[:80]}")

    # Show early recommendations if available
    rec_path = _bus_path(job["inv_dir"], BUS_RECOMMENDATIONS)
    if Path(rec_path).exists():
        rec = Path(rec_path).read_text().strip()
        if rec and "Waiting for initial findings" not in rec:
            lines.append(f"\n📋 EARLY RECOMMENDATIONS:\n{rec[:2000]}")

    if job["phase"] in ("investigating", "orchestrating"):
        lines.append(f"\n💡 Tip: Use investigation_feed('{resolved}', '<cli output>') to share findings with the investigation.")

    return "\n".join(lines)


@mcp.tool()
def investigation_result(job_id: str) -> str:
    """Get the final report from a completed investigation."""
    resolved = _resolve_job_id(job_id)
    if not resolved:
        return f"Unknown job: {job_id}" if _jobs else "No investigations have been started."
    job = _jobs[resolved]
    rp = job.get("report_path")
    if rp and Path(rp).exists():
        content = Path(rp).read_text()
        # Truncate to avoid crashing MCP transport on large reports
        MAX_RESULT = 60000
        if len(content) > MAX_RESULT:
            content = content[:MAX_RESULT] + f"\n\n---\n_Report truncated at {MAX_RESULT} chars. Full report at: {rp}_\n"
        return content
    # Partial results
    parts = [f"Investigation {job_id} — not yet complete (phase: {job['phase']})\n"]
    for name, child in job["children"].items():
        for label, path in [("validated", child["validated_path"]), ("findings", child["findings_path"])]:
            if Path(path).exists():
                parts.append(f"## {name} ({label})\n{Path(path).read_text()[:3000]}\n")
                break
    return "\n".join(parts)


@mcp.tool()
def stop_investigation(job_id: str) -> str:
    """Stop all processes in an investigation."""
    resolved = _resolve_job_id(job_id)
    if not resolved:
        return f"Unknown job: {job_id}" if _jobs else "No investigations have been started."
    job = _jobs[resolved]
    job["phase"] = "stopped"
    killed = []
    for name, child in job["children"].items():
        for key in ["proc", "validator_proc"]:
            proc = child.get(key)
            if isinstance(proc, subprocess.Popen):
                _kill_proc(proc)
                killed.append(f"{name}/{key}")
    if job.get("orchestrator_proc"):
        _kill_proc(job["orchestrator_proc"])
        killed.append("orchestrator")
    if job.get("head_proc"):
        _kill_proc(job["head_proc"])
        killed.append("head-agent")
    if job.get("visual_proc"):
        _kill_proc(job["visual_proc"])
        killed.append("visual-report")
    return f"Stopped {len(killed)} processes: {', '.join(killed)}"


@mcp.tool()
def investigation_feed(job_id: str, content: str, context: str = "") -> str:
    """Feed CLI output or findings back into a running investigation.
    The head agent and all children will see this input.

    Args:
        job_id: Investigation job ID
        content: CLI output, log snippet, or any findings to share
        context: Optional context about what command produced this output
    """
    resolved = _resolve_job_id(job_id)
    if not resolved:
        return f"Unknown job: {job_id}" if _jobs else "No investigations have been started."
    job = _jobs[resolved]
    entry = {"from": "user", "context": context, "content": content, "ts": time.strftime("%H:%M:%S")}
    _append_bus(job["inv_dir"], BUS_USER_INPUT, entry)
    # Also add as a directive so children pick it up
    directive = {"from": "user", "to": "all", "directive": f"USER PROVIDED INPUT: {context}\n{content}"}
    _append_bus(job["inv_dir"], BUS_DIRECTIVES, directive)
    return f"✅ Input fed to investigation {resolved}. Head agent and children will incorporate this."


def _open_writer_progress(log_path: str, label: str, pid: int):
    """Open a Ghostty tab tailing the writer log for live progress."""
    launcher = f"/tmp/kiro_writer_{label}_{os.getpid()}.sh"
    Path(launcher).write_text(
        f'#!/bin/bash\n'
        f'printf \'\\e]2;✉️📝 Correspondence: {label}\\a\'\n'
        f'printf \'\\033[1;35m\'\n'
        f'echo "╔══════════════════════════════════════════════════════════════╗"\n'
        f'printf "║  ✉️📝  Correspondence Writer — %-30s  ║\\n" "{label}"\n'
        f'echo "╚══════════════════════════════════════════════════════════════╝"\n'
        f'printf \'\\033[0m\'\n'
        f'echo ""\n'
        f'touch "{log_path}"\n'
        f'tail -f "{log_path}" &\n'
        f'TAIL_PID=$!\n'
        f'while kill -0 {pid} 2>/dev/null; do sleep 2; done\n'
        f'sleep 2\n'
        f'kill $TAIL_PID 2>/dev/null\n'
        f'echo ""\n'
        f'printf \'\\033[1;35m━━━ ✅ Correspondence complete ━━━\\033[0m\\n\'\n'
        f'sleep 3\n'
        f'TAB_TITLE="✉️📝 Correspondence: {label}"\n'
        f'osascript -e "tell application \\"Ghostty\\"" '
        f'-e "repeat with w in windows" '
        f'-e "repeat with t in tabs of w" '
        f'-e "if name of t contains \\"$TAB_TITLE\\" then" '
        f'-e "close tab t" -e "return" '
        f'-e "end if" -e "end repeat" -e "end repeat" '
        f'-e "end tell" 2>/dev/null\n'
    )
    os.chmod(launcher, 0o755)
    _open_ghostty_tab(launcher)


def _monitor_correspondence(job_id: str):
    """Background thread: wait for writer processes to finish, collect results."""
    job = _jobs[job_id]
    import time
    while True:
        time.sleep(5)
        if job["phase"] == "stopped":
            return
        all_done = all(_is_done(w["proc"]) for w in job["writers"])
        if all_done:
            break
    job["phase"] = "complete"


@mcp.tool()
def write_correspondence(
    findings: str,
    customer_context: str = "",
    case_id: str = "",
    first_correspondence: bool | None = None,
    tab_id: str = "",
) -> str:
    """Generate a professional customer correspondence from investigation findings.

    Args:
        findings: Technical findings and root cause analysis
        customer_context: What the customer reported (optional if read_tab=True)
        case_id: Optional case ID for reference
        first_correspondence: True=introduce yourself, False=follow-up, None=generate both versions
        tab_id: Browser tab ID (from list_tabs output, e.g. "ID:12345:67890"). If provided, reads that tab directly for case context. If empty, a native tab picker dialog will appear for the user to select the case tab.
    """
    job_id = str(uuid.uuid4())[:8]
    work_dir = os.getcwd()
    out_dir = str(Path(work_dir) / "correspondence")
    os.makedirs(out_dir, exist_ok=True)

    if tab_id:
        tab_instruction = (
            f"FIRST: Use read_tab_content with id=\"{tab_id}\" to read the customer's case tab. "
            f"The content may be truncated — if so, call read_tab_content again with increasing startIndex "
            f"until you have the full case context. "
            f"Use the content to understand their questions, context, and what needs to be addressed.\n\n"
        )
    else:
        tab_instruction = ""

    if first_correspondence is None:
        versions = [("first", True), ("followup", False)]
    else:
        versions = [("first" if first_correspondence else "followup", first_correspondence)]

    writers = []
    for label, is_first in versions:
        out_path = str(Path(out_dir) / f"correspondence_{case_id or 'draft'}_{label}.md")
        log_path = str(Path(out_dir) / f"writer_{label}.log")
        intro_type = "FIRST CORRESPONDENCE (introduce yourself)" if is_first else "FOLLOW-UP CORRESPONDENCE"

        task = (
            f"{tab_instruction}"
            f"{CORRESPONDENCE_STYLE}\n\n"
            f"This is a {intro_type}.\n\n"
            f"## Customer Context\n{customer_context}\n\n"
            f"## Technical Findings\n{findings}\n\n"
            f"Write the correspondence to: {out_path}"
        )
        proc = _spawn_kiro(CHILD_AGENT, task, work_dir, log_path)
        _open_writer_progress(log_path, label, proc.pid)
        writers.append({"label": label, "proc": proc, "out_path": out_path, "log_path": log_path})

    job = {
        "job_id": job_id, "phase": "writing", "writers": writers,
        "work_dir": work_dir, "out_dir": out_dir,
    }
    _jobs[job_id] = job

    thread = threading.Thread(target=_monitor_correspondence, args=(job_id,), daemon=True)
    thread.start()

    labels = ", ".join(l for l, _ in versions)
    return (
        f"✉️ Correspondence {job_id} started!\n"
        f"📂 Output: {out_dir}\n"
        f"📝 Versions: {labels}\n"
        f"📊 Progress tabs opened in Ghostty.\n\n"
        f"Writers are running in the background. Use correspondence_status('{job_id}') to check progress."
    )


@mcp.tool()
def correspondence_status(job_id: str) -> str:
    """Check progress of correspondence writing. Returns content when complete."""
    if job_id not in _jobs:
        return f"Unknown job: {job_id}"
    job = _jobs[job_id]
    if job.get("writers") is None:
        return f"Job {job_id} is not a correspondence job."
    parts = [f"Correspondence {job_id} — Phase: {job['phase']}\n"]
    for w in job["writers"]:
        done = _is_done(w["proc"])
        exists = Path(w["out_path"]).exists()
        status = "✅" if done else "⏳"
        file_status = "📄" if exists else "⬜"
        parts.append(f"  {w['label']}: {status} output={file_status}")
    all_done = all(_is_done(w["proc"]) for w in job["writers"])
    if all_done:
        parts.append("")
        for w in job["writers"]:
            if Path(w["out_path"]).exists():
                parts.append(f"### {w['label'].upper()} VERSION\n\n{Path(w['out_path']).read_text()}")
            else:
                parts.append(f"### {w['label'].upper()} VERSION\n\n(failed — check {w['log_path']})")
    return "\n".join(parts)


@mcp.tool()
def generate_report(raw_findings: str, report_type: str = "investigation", case_id: str = "") -> str:
    """Generate a structured report from raw findings. report_type: investigation|bug_reproduction|executive_summary"""
    job_id = str(uuid.uuid4())[:8]
    work_dir = os.getcwd()
    out_dir = str(Path(work_dir) / "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = str(Path(out_dir) / f"{report_type}_{case_id or 'draft'}.md")
    log_path = str(Path(out_dir) / "reporter.log")
    task = f"Generate a {report_type} report from:\n\n{raw_findings}\n\nWrite to: {out_path}"
    proc = _spawn_kiro(CHILD_AGENT, task, work_dir, log_path)

    job = {"job_id": job_id, "phase": "writing", "writers": [
        {"label": report_type, "proc": proc, "out_path": out_path, "log_path": log_path}
    ], "work_dir": work_dir, "out_dir": out_dir}
    _jobs[job_id] = job

    def _monitor():
        import time
        while proc.poll() is None:
            time.sleep(5)
        job["phase"] = "complete"

    threading.Thread(target=_monitor, daemon=True).start()
    return (
        f"📊 Report {job_id} started!\n"
        f"📂 Output: {out_path}\n\n"
        f"Use correspondence_status('{job_id}') to check progress."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
