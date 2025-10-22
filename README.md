# Cursor Chat Exporter

Export Cursor IDE chat history to markdown files with complete tool actions, context, and conversation data. Designed for backup, documentation, and auditing.

## Features

- **Complete Export**: Full conversation history with zero truncation of diffs, terminal output, or tool results
- **Rich Context**: Includes git diffs, linting errors, web searches, attached files, cursor rules, and token usage
- **Smart Filenames**: Auto-generated from conversation content with timestamps and workspace identifiers
- **File Tracking**: Preserves manual edits via checksums with timestamped backups
- **Safe & Local**: Read-only database access, no data transmission, works offline
- **Cross-Platform**: macOS, Windows, Linux support with no external dependencies

## Requirements

Python 3.7+ (uses standard library only, no external dependencies)

## Quick Start

```bash
# Clone repository
git clone <repository-url>
cd cursor-exporter/main

# Basic export
python export-cursor.py

# Custom output directory
python export-cursor.py /path/to/exports

# Verbose mode
python export-cursor.py --verbose

# View help
python export-cursor.py --help
```

## Output Structure

Flat directory with markdown files named: `YYYY-MM-DD-descriptive-title-workspaceHash.md`

```
cursor_exports/
├── .export_metadata.json          # Checksum tracking
├── terminal-history.md            # Last 50 shell commands
└── 2025-09-18-conversation-21f0973c.md
```

Each conversation file includes complete, untruncated content: messages, code diffs, terminal output, git context, linting errors, web searches, cursor rules, token usage, and tool actions. Designed for grep-ability and audit trails.

## File Modification Tracking

Exported files are SHA256-tracked. If you manually edit an exported markdown file, the next export creates a timestamped backup (e.g., `filename.modified_20240318_143022.md`) before updating. Use `--status` to view tracking information.

## Automation with Cron

```bash
# Daily at 2 AM
0 2 * * * cd /path/to/cursor-exporter/main && python3 export-cursor.py /path/to/exports >> /path/to/export.log 2>&1

# Every minute (near real-time)
* * * * * cd /path/to/cursor-exporter/main && python3 export-cursor.py /path/to/exports >> /path/to/export.log 2>&1

# With error notifications
* * * * * cd /path/to/cursor-exporter/main && /usr/local/bin/python3 export-cursor.py /path/to/exports >> /path/to/export.log 2>&1 || /path/to/notify.sh "Export failed"
```

Use full paths for python3 (find with `which python3` or use pyenv paths like `~/.local/share/pyenv/shims/python3`).

## Troubleshooting

**No databases found:** Ensure Cursor IDE is installed and you've used the AI chat feature at least once.

**Permission errors:** Check read access to Cursor's data directory:
- macOS: `~/Library/Application Support/Cursor/User/globalStorage`
- Windows: `%APPDATA%/Cursor/User/globalStorage`
- Linux: `~/.config/Cursor/User/globalStorage`

**Empty exports:** Database structure may have changed. Check exported JSON in markdown files.

**Metadata issues:** Delete `.export_metadata.json` to reset tracking or use `--status` to inspect.

## Security & Privacy

Read-only database access, no network transmission, all data stays local. Handle exported markdown files according to your privacy requirements.

## License

MIT License - see LICENSE file for details
