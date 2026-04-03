"""Integration test: simulate the PDF generation step at the end of a profound investigation.

Exercises the exact code path: _autolink_internal → _md_to_pdf_convert (auto_linkify + mermaid + pandoc + weasyprint).
Mocks nothing in the PDF pipeline — only skips the actual kiro-cli agent spawning.
"""
import os, sys, tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

# Import the real server functions used in the PDF generation step
from md_to_pdf import convert as _md_to_pdf_convert
from md_to_pdf import auto_linkify

# _autolink_internal lives in server.py but importing it starts the MCP server.
# Inline a minimal copy for testing the chain.
import re

def _autolink_internal(text: str) -> str:
    def _link(pattern, url_tpl):
        def repl(m):
            start = m.start()
            before = text[max(0, start-2):start]
            if '[' in before or '](' in before:
                return m.group(0)
            url = url_tpl.format(*m.groups())
            return f'{m.group(0)}  \n  **🔗 {url}**'
        return re.sub(pattern, repl, text)
    text = _link(r'Sage post #(\d+)', 'https://sage.amazon.dev/posts/{}')
    text = _link(r'COE-(\d+)', 'https://coe.a2z.com/coe/{}')
    text = _link(r'(?<!\()(w\.amazon\.com/bin/view/\S+)', 'https://{}')
    return text


def _md_to_pdf(md_path: str, pdf_path: str) -> bool:
    """Exact replica of server._md_to_pdf minus the subprocess.Popen(['open', ...])."""
    try:
        content = Path(md_path).read_text()
        content = _autolink_internal(content)
        linked_path = md_path.rsplit(".", 1)[0] + "_linked.md"
        Path(linked_path).write_text(content)
        _md_to_pdf_convert(linked_path, pdf_path)
        return Path(pdf_path).exists()
    except Exception as e:
        print(f"PDF error: {e}", flush=True, file=sys.stderr)
        return False


# ── Mock visual report content (what the visual-report agent would produce) ──

MOCK_VISUAL_REPORT = """# Investigation Report: ECS Task Failures in prod-us-east-1

## Executive Summary

Between 14:00–15:30 UTC on 2026-03-29, the order-processing ECS service experienced repeated task failures
due to an OOM condition triggered by a memory leak in the connection pool. Ticket V9182736450 tracks the incident.
Related Sage post #48291 documents the known connection pool issue. COE-2847 was filed.

## Architecture

```mermaid
flowchart TD
    ALB[Application Load Balancer] --> ECS[ECS Service: order-processing]
    ECS --> RDS[(Aurora PostgreSQL)]
    ECS --> Redis[(ElastiCache Redis)]
    ECS --> SQS[SQS: order-events]
    SQS --> Lambda[Processing Lambda]
    Lambda --> DDB[(DynamoDB: order-status)]
    ECS --> CW[CloudWatch Metrics]
```

## Incident Timeline

```mermaid
gantt
    title ECS OOM Incident Timeline
    dateFormat YYYY-MM-DD HH:mm
    section Detection
    Memory alarm fired        :a1, 2026-03-29 14:00 UTC, 3min
    On-call paged             :a2, 2026-03-29 14:03 UTC, 2min
    section Diagnosis
    Checked ECS task logs     :b1, 2026-03-29 14:05 UTC, 8min
    Identified OOM pattern    :b2, 2026-03-29 14:13 UTC, 5min
    Found connection leak     :b3, 2026-03-29 14:18 UTC, 7min
    section Mitigation
    Deployed hotfix           :c1, 2026-03-29 14:25 UTC, 12min
    Restarted ECS tasks       :c2, 2026-03-29 14:37 UTC, 5min
    section Recovery
    Memory stabilized         :d1, 2026-03-29 14:42 UTC, 20min
    All tasks healthy         :d2, 2026-03-29 15:02 UTC, 28min
```

## Error Pattern

| Time | Task ID | Exit Code | Memory (MB) | Status |
|------|---------|-----------|-------------|--------|
| 14:01 | abc123 | 137 | 2048/2048 | OOMKilled |
| 14:08 | def456 | 137 | 2048/2048 | OOMKilled |
| 14:15 | ghi789 | 137 | 2048/2048 | OOMKilled |
| 14:22 | jkl012 | 137 | 2048/2048 | OOMKilled |
| 14:37 | mno345 | 0 | 512/2048 | Healthy (post-fix) |

## Root Cause

The `asyncpg` connection pool was not releasing connections on timeout, causing unbounded memory growth:

```python
# BEFORE (leaking connections)
pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=5, max_size=100)

async def query(sql: str):
    conn = await pool.acquire()
    # Missing: release on timeout/error
    return await conn.fetch(sql)
```

```python
# AFTER (fixed)
pool = await asyncpg.create_pool(
    dsn=DATABASE_URL, min_size=5, max_size=20,
    max_inactive_connection_lifetime=300,
    command_timeout=30,
)

async def query(sql: str):
    async with pool.acquire() as conn:  # context manager ensures release
        return await conn.fetch(sql)
```

## Memory Profile

```mermaid
sequenceDiagram
    participant App as ECS Task
    participant Pool as Connection Pool
    participant DB as Aurora PostgreSQL

    App->>Pool: acquire()
    Pool->>DB: Open connection
    DB-->>Pool: Connection established
    Pool-->>App: Connection handle
    App->>DB: SELECT * FROM orders
    DB-->>App: Results (50MB)
    Note over App,Pool: Connection NOT released (bug)
    App->>Pool: acquire() again
    Pool->>DB: Open NEW connection
    Note over App: Memory grows unbounded
```

## Verification

```bash
aws ecs describe-services \\
    --cluster prod-us-east-1 \\
    --services order-processing \\
    --query 'services[0].deployments[0].runningCount'
```

Confirmed via w.amazon.com/bin/view/OrderService/Runbooks/OOM that the fix matches the documented remediation.

> **Next Steps**: Add connection pool metrics to CloudWatch dashboard. Set memory utilization alarm at 75% threshold.
"""


class TestProfoundInvestigationPDF:
    """Test the PDF generation step that runs at the end of _orchestrate_inner."""

    def test_full_pipeline_produces_valid_pdf(self):
        """Simulate: visual-report agent writes markdown → _md_to_pdf converts it."""
        with tempfile.TemporaryDirectory(prefix="inv_test_") as inv_dir:
            visual_report_path = str(Path(inv_dir) / "visual_report.md")
            pdf_path = str(Path(inv_dir) / "visual_report.pdf")

            # Simulate visual-report agent output
            Path(visual_report_path).write_text(MOCK_VISUAL_REPORT)

            # Run the exact PDF generation code path from _orchestrate_inner
            result = _md_to_pdf(visual_report_path, pdf_path)

            assert result is True, "PDF generation returned False"
            assert Path(pdf_path).exists(), "PDF file not created"
            assert Path(pdf_path).stat().st_size > 10_000, f"PDF too small: {Path(pdf_path).stat().st_size} bytes"

    def test_autolink_chain_in_report(self):
        """Verify both link layers work: _autolink_internal (Sage/COE) + auto_linkify (V-tickets)."""
        content = MOCK_VISUAL_REPORT
        # Layer 1: _autolink_internal
        content = _autolink_internal(content)
        assert "sage.amazon.dev/posts/48291" in content, "Sage post not linked"
        assert "coe.a2z.com/coe/2847" in content, "COE not linked"
        assert "https://w.amazon.com/bin/view/OrderService/Runbooks/OOM" in content, "Wiki not linked"

        # Layer 2: auto_linkify
        content = auto_linkify(content)
        assert "t.corp.amazon.com/V9182736450" in content, "V-ticket not linked"

    def test_intermediate_linked_file_created(self):
        """Verify _autolink_internal output is written to _linked.md before PDF conversion."""
        with tempfile.TemporaryDirectory(prefix="inv_test_") as inv_dir:
            md_path = str(Path(inv_dir) / "visual_report.md")
            pdf_path = str(Path(inv_dir) / "visual_report.pdf")
            Path(md_path).write_text(MOCK_VISUAL_REPORT)

            _md_to_pdf(md_path, pdf_path)

            linked_path = str(Path(inv_dir) / "visual_report_linked.md")
            assert Path(linked_path).exists(), "_linked.md not created"
            linked_content = Path(linked_path).read_text()
            assert "sage.amazon.dev" in linked_content, "Sage link missing from linked file"

    def test_pdf_not_corrupted(self):
        """Verify PDF starts with the %PDF magic bytes."""
        with tempfile.TemporaryDirectory(prefix="inv_test_") as inv_dir:
            md_path = str(Path(inv_dir) / "report.md")
            pdf_path = str(Path(inv_dir) / "report.pdf")
            Path(md_path).write_text(MOCK_VISUAL_REPORT)

            _md_to_pdf(md_path, pdf_path)

            header = Path(pdf_path).read_bytes()[:5]
            assert header == b"%PDF-", f"Invalid PDF header: {header}"
