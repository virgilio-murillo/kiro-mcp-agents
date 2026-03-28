"""Kiro Agents MCP server — orchestrates parallel kiro-cli investigations."""

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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

NODE_INSTRUCTION = (
    "\n\nPROGRESS TRACKING: As you work, append a short progress line (max 3 words) to {nodes_path} "
    "using this exact format: HH:MM:SS|Three Word Summary\n"
    "Example: echo \"$(date +%H:%M:%S)|Searching AWS docs\" >> {nodes_path}\n"
    "Add a node each time you start a meaningfully different step. Do NOT add more than one every 30 seconds."
)

CHILDREN = {
    "c1-internet": (
        "Investigate using WEB SEARCH ONLY. Search for related issues, solutions, "
        "workarounds, blog posts, Stack Overflow, GitHub issues.\n\n"
        "Topic: {description}\n\nWrite findings to: {findings_path}"
    ),
    "c2-kb": (
        "Investigate using KNOWLEDGE tools ONLY. Use the `knowledge` tool to search indexed "
        "knowledge bases AND use the `search_lessons` tool to search lessons learned. "
        "Try multiple search queries with different keywords.\n\n"
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

ORCHESTRATOR_TASK = (
    "Read ALL validated findings from {inv_dir}. Each subdirectory "
    "(c1-internet, c2-kb, c3-context, c4-docs, c5-internal) contains a validated.md file.\n\n"
    "Original topic: {description}\n\n"
    "Cross-reference, resolve contradictions, fill gaps with your own investigation. "
    "Write the final report to: {report_path}"
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
    "- Mermaid architecture/flow diagrams (```mermaid blocks)\n"
    "- Step-by-step implementation instructions with CLI commands\n"
    "- AWS Console walkthrough (Service → Section → Settings)\n"
    "- Summary tables\n\n"
    "Write the visual report to: {visual_report_path}"
    "\n\nPROGRESS TRACKING: Append progress lines to {nodes_path} "
    "using: echo \"$(date +%H:%M:%S)|Three Word Summary\" >> {nodes_path}\n"
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
    cmd = ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", "--agent", agent, "--wrap=never"]
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
    status = {"phase": job["phase"], "children": {}}
    for name, child in job["children"].items():
        proc = child["proc"]
        inv_status = "done" if _is_done(proc) else "running"
        vp = child.get("validator_proc")
        val_status = "pending"
        if vp:
            val_status = "done" if _is_done(vp) else "running"
        status["children"][name] = {
            "inv_status": inv_status,
            "has_findings": Path(child["findings_path"]).exists(),
            "val_status": val_status,
            "has_validated": Path(child["validated_path"]).exists(),
        }
    if job.get("orchestrator_proc"):
        status["orchestrator"] = "done" if _is_done(job["orchestrator_proc"]) else "running"
    if job.get("visual_proc"):
        status["visual"] = "done" if _is_done(job["visual_proc"]) else "running"
        status["has_pdf"] = Path(job.get("pdf_path", "")).exists() if job.get("pdf_path") else False
    (Path(job["inv_dir"]) / "status.json").write_text(json.dumps(status, indent=2))


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


def _md_to_pdf(md_path: str, pdf_path: str) -> bool:
    """Convert markdown (with mermaid) to PDF using md-to-pdf, then open it."""
    try:
        subprocess.run(
            [
                "npx", "--yes", "md-to-pdf", md_path,
                "--highlight-style", "monokai",
                "--css", "pre { background: #272822; padding: 1em; border-radius: 6px; } code { color: #f8f8f2; }",
                "--pdf-options", '{"printBackground": true}',
            ],
            capture_output=True, timeout=120,
        )
        # md-to-pdf outputs alongside the .md file
        default_pdf = md_path.rsplit(".", 1)[0] + ".pdf"
        if Path(default_pdf).exists():
            if default_pdf != pdf_path:
                Path(default_pdf).rename(pdf_path)
            subprocess.Popen(["open", pdf_path])
            return True
    except Exception:
        pass
    return False


def _orchestrate(job_id: str):
    """Background thread: manage investigation phases."""
    job = _jobs[job_id]

    # Phase 1+2: Wait for investigators, spawn validators
    while True:
        import time
        time.sleep(POLL_INTERVAL)
        if job["phase"] == "stopped":
            return

        for name, child in job["children"].items():
            if _is_done(child["proc"]) and not child.get("validator_proc"):
                if Path(child["findings_path"]).exists():
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

    # Phase 3: Orchestrator
    job["phase"] = "orchestrating"
    report_path = str(Path(job["inv_dir"]) / "final_report.md")
    orch_log = str(Path(job["inv_dir"]) / "orchestrator.log")
    orch_nodes = str(Path(job["inv_dir"]) / "orchestrator_nodes")
    Path(orch_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Reading findings\n")
    orch_task = ORCHESTRATOR_TASK.format(
        inv_dir=job["inv_dir"], description=job["description"],
        report_path=report_path, nodes_path=orch_nodes,
    )
    job["orchestrator_proc"] = _spawn_kiro(ORCHESTRATOR_AGENT, orch_task, job["work_dir"], orch_log, model=LARGE_CONTEXT_MODEL)
    job["report_path"] = report_path
    _update_status(job)

    while not _is_done(job["orchestrator_proc"]):
        import time
        time.sleep(POLL_INTERVAL)
        _update_status(job)
        if job["phase"] == "stopped":
            return

    # Phase 4: Visual Report + PDF
    job["phase"] = "visualizing"
    visual_report_path = str(Path(job["inv_dir"]) / "visual_report.md")
    visual_log = str(Path(job["inv_dir"]) / "visual_report.log")
    visual_nodes = str(Path(job["inv_dir"]) / "visual_nodes")
    Path(visual_nodes).write_text(f"{time.strftime('%H:%M:%S')}|Reading report\n")
    visual_task = VISUAL_REPORT_TASK.format(
        report_path=report_path, description=job["description"],
        visual_report_path=visual_report_path, nodes_path=visual_nodes,
    )
    job["visual_proc"] = _spawn_kiro(VISUAL_REPORT_AGENT, visual_task, job["work_dir"], visual_log, model=LARGE_CONTEXT_MODEL)
    job["visual_report_path"] = visual_report_path
    _update_status(job)

    while not _is_done(job["visual_proc"]):
        import time
        time.sleep(POLL_INTERVAL)
        _update_status(job)
        if job["phase"] == "stopped":
            return

    # Convert to PDF and open
    pdf_path = str(Path(job["inv_dir"]) / "visual_report.pdf")
    if Path(visual_report_path).exists():
        _md_to_pdf(visual_report_path, pdf_path)
    job["pdf_path"] = pdf_path

    job["phase"] = "complete"
    _update_status(job)


# ── MCP Tools ──────────────────────────────────────────────────────

@mcp.tool()
def profound_investigation(description: str, work_dir: str) -> str:
    """Launch a parallel investigation with 8 child agents and an orchestrator.
    Returns immediately. Opens a live dashboard in a separate terminal.
    Use investigation_status to check progress and investigation_result to get the report.

    Args:
        description: What to investigate (case description, error, topic)
        work_dir: Working directory for the investigation
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
        ) + NODE_INSTRUCTION.format(nodes_path=nodes_path)
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
        }

    job = {
        "job_id": job_id, "description": description, "work_dir": work_dir,
        "inv_dir": inv_dir, "phase": "investigating", "children": children,
        "orchestrator_proc": None, "report_path": None,
    }
    _jobs[job_id] = job
    _update_status(job)

    # Open live dashboard in a new terminal
    _open_dashboard(inv_dir, job_id)
    job["dashboard_opened"] = True

    thread = threading.Thread(target=_orchestrate, args=(job_id,), daemon=True)
    thread.start()

    return (
        f"🔍 Investigation {job_id} launched!\n"
        f"📂 Directory: {inv_dir}\n"
        f"📊 Dashboard opened in Ghostty.\n\n"
        f"Use investigation_status('{job_id}') to check progress.\n"
        f"Use investigation_result('{job_id}') to get the report when complete.\n"
        f"Use stop_investigation('{job_id}') to abort."
    )


@mcp.tool()
def investigation_status(job_id: str) -> str:
    """Check progress of a running investigation."""
    resolved = _resolve_job_id(job_id)
    if not resolved:
        return f"Unknown job: {job_id}" if _jobs else "No investigations have been started."
    job = _jobs[resolved]
    lines = [f"Investigation {job_id} — Phase: {job['phase']}", ""]
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
        return Path(rp).read_text()
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
    if job.get("visual_proc"):
        _kill_proc(job["visual_proc"])
        killed.append("visual-report")
    return f"Stopped {len(killed)} processes: {', '.join(killed)}"


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
