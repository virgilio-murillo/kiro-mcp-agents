# kiro-mcp-agents

MCP server that orchestrates parallel investigations with a live [Ghostty](https://ghostty.org/) terminal dashboard. macOS only.

Part of the [kiro-mcp-servers](https://github.com/virgilio-murillo/kiro-mcp-servers) collection.

## Tools

| Tool | Description |
|------|-------------|
| `profound_investigation` | Launch parallel investigation with 8 child agents + orchestrator |
| `investigation_status` | Check progress of a running investigation |
| `investigation_result` | Get the final report from a completed investigation |
| `investigation_feed` | Feed CLI output or findings into a running investigation |
| `stop_investigation` | Stop all processes in an investigation |
| `write_correspondence` | Generate professional customer correspondence |
| `generate_report` | Generate structured report from raw findings |

## Requirements

- Python 3.10+
- macOS (Ghostty + AppleScript integration)
- `mcp[cli]>=1.0.0`

## Install

```bash
pip install -e .
```

## Usage

```bash
kiro-agents
```
