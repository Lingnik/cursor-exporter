#!/usr/bin/env python3
"""
Cursor Chat Exporter - Corrected Version
Exports Cursor IDE chat conversations to markdown files.

Key findings:
- Conversation data is in GLOBAL database, not workspace databases
- Uses cursorDiskKV table, not ItemTable  
- composerData:{cid} contains thread metadata
- bubbleId:{cid}:% contains conversation messages
- type: 1 = User, type: 2 = Assistant
"""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

LOCK_FILE = '/tmp/export-cursor.lock'


def get_cursor_base_path() -> str:
    """Return base path to Cursor's user data directory."""
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/Cursor/User')
    elif sys.platform.startswith('win'):
        appdata = os.environ.get('APPDATA') or os.path.expanduser(
            '~\\AppData\\Roaming')
        return os.path.join(appdata, 'Cursor', 'User')
    else:
        xdg = os.environ.get(
            'XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        return os.path.join(xdg, 'Cursor', 'User')


def get_global_cursor_db_path() -> str:
    """Return path to global Cursor database containing conversation data."""
    return os.path.join(get_cursor_base_path(), 'globalStorage', 'state.vscdb')


def get_workspace_db_paths() -> List[str]:
    """Return paths to all workspace-specific Cursor databases."""
    import glob
    base_path = get_cursor_base_path()
    workspace_pattern = os.path.join(base_path, 'workspaceStorage', '*', 'state.vscdb')
    return glob.glob(workspace_pattern)


def connect_db_readonly(db_path: str) -> sqlite3.Connection:
    """Connect to SQLite database in read-only mode."""
    uri = f"file:{urllib.parse.quote(db_path)}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def get_composer_threads(conn: sqlite3.Connection) -> List[Tuple[str, Dict[str, Any]]]:
    """Get all composer threads with their metadata from a single database."""
    threads = []
    cursor = conn.cursor()
    
    # Check cursorDiskKV table for composerData entries
    try:
        cursor.execute("""
            SELECT substr(key, length('composerData:')+1) AS cid, value
            FROM cursorDiskKV
            WHERE key LIKE 'composerData:%'
        """)
        
        for cid, value_blob in cursor.fetchall():
            try:
                if value_blob is None:
                    continue
                data = json.loads(value_blob)
                threads.append((cid, data))
            except (json.JSONDecodeError, TypeError):
                continue
    except sqlite3.OperationalError:
        pass  # Table doesn't exist or is empty
    
    # Check ItemTable for composer.composerData with allComposers array
    try:
        cursor.execute("""
            SELECT value FROM ItemTable 
            WHERE key = 'composer.composerData'
        """)
        
        result = cursor.fetchone()
        if result and result[0]:
            try:
                data = json.loads(result[0])
                if isinstance(data, dict) and 'allComposers' in data:
                    for composer in data['allComposers']:
                        if isinstance(composer, dict) and 'composerId' in composer:
                            cid = composer['composerId']
                            threads.append((cid, composer))
            except (json.JSONDecodeError, TypeError):
                pass
    except sqlite3.OperationalError:
        pass  # Table doesn't exist
    
    return threads


def get_all_composer_threads(verbose: bool = False) -> List[Tuple[str, Dict[str, Any]]]:
    """Get all composer threads from global and workspace databases."""
    all_threads = {}  # Use dict to deduplicate by cid
    
    # Get from global database
    global_db = get_global_cursor_db_path()
    if os.path.exists(global_db):
        if verbose:
            print(f"Searching global database...")
        try:
            conn = connect_db_readonly(global_db)
            threads = get_composer_threads(conn)
            for cid, data in threads:
                all_threads[cid] = data
            conn.close()
            if verbose:
                print(f"  Found {len(threads)} threads in global database")
        except Exception as e:
            if verbose:
                print(f"  Error reading global database: {e}")
    
    # Get from workspace databases
    workspace_dbs = get_workspace_db_paths()
    if verbose:
        print(f"Searching {len(workspace_dbs)} workspace databases...")
    
    for db_path in workspace_dbs:
        try:
            conn = connect_db_readonly(db_path)
            threads = get_composer_threads(conn)
            for cid, data in threads:
                # Keep the most recently updated version
                if cid not in all_threads or data.get('lastUpdatedAt', 0) > all_threads[cid].get('lastUpdatedAt', 0):
                    all_threads[cid] = data
            conn.close()
        except Exception:
            pass  # Skip databases that can't be opened
    
    if verbose:
        print(f"Total unique threads found: {len(all_threads)}")
    
    # Sort by creation date (newest first)
    sorted_threads = sorted(all_threads.items(), key=lambda x: x[1].get('createdAt', 0), reverse=True)
    return sorted_threads


def get_thread_bubbles(conn: sqlite3.Connection, cid: str) -> List[Dict[str, Any]]:
    """Get all conversation bubbles (messages) for a thread."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT key, value
        FROM cursorDiskKV
        WHERE key LIKE ?
        ORDER BY COALESCE(json_extract(value,'$.createdAt'),0) ASC
    """, (f"bubbleId:{cid}:%",))

    bubbles = []
    for _, value_blob in cursor.fetchall():
        try:
            if value_blob is None:
                continue
            bubble = json.loads(value_blob)
            bubbles.append(bubble)
        except (json.JSONDecodeError, TypeError):
            continue

    return bubbles


def get_bubbles_batch(conn: sqlite3.Connection, cid_set: set) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch bubbles for multiple CIDs in a single table scan.

    A single pass over all bubbleId:* rows is ~250ms, far cheaper than
    one LIKE query per CID (~34ms each × N threads).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT key, value
        FROM cursorDiskKV
        WHERE key LIKE 'bubbleId:%'
        ORDER BY COALESCE(json_extract(value, '$.createdAt'), 0) ASC
    """)

    result: Dict[str, List[Dict[str, Any]]] = {}
    for key, value_blob in cursor:
        # key format: bubbleId:{cid}:{bubble_id}
        rest = key[9:]  # strip 'bubbleId:'
        colon = rest.find(':')
        if colon == -1:
            continue
        cid = rest[:colon]
        if cid not in cid_set or value_blob is None:
            continue
        try:
            bubble = json.loads(value_blob)
        except (json.JSONDecodeError, TypeError):
            continue
        result.setdefault(cid, []).append(bubble)

    return result


def extract_message_content(bubble: Dict[str, Any]) -> str:
    """Extract text content from a bubble."""
    content = (
        bubble.get('content') or
        bubble.get('text') or
        bubble.get('richText') or
        bubble.get('message') or
        ''
    )

    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=2)

    return str(content).strip()


def extract_thinking_content(bubble: Dict[str, Any]) -> str:
    """Extract thinking content from a bubble if present."""
    thinking = bubble.get('thinking', {})
    if isinstance(thinking, dict):
        return thinking.get('text', '').strip()
    return ""


def get_message_context(conn: sqlite3.Connection, cid: str, bubble_id: str) -> Dict[str, Any]:
    """Get message context data for a specific bubble ID."""
    cursor = conn.cursor()
    context_key = f"messageRequestContext:{cid}:{bubble_id}"

    cursor.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?", (context_key,))
    result = cursor.fetchone()

    if result and result[0]:
        try:
            return json.loads(result[0])
        except json.JSONDecodeError:
            return {}
    return {}


def truncate_string(s: str, max_len: int, suffix: str = '...') -> str:
    """Truncate string to max length with suffix if needed."""
    if len(s) <= max_len:
        return s
    return s[:max_len - len(suffix)] + suffix


def format_code_block(code: str, prefix: str = '', max_lines: int = None) -> List[str]:
    """Format a code block with optional prefix. Includes ALL lines for audit completeness.

    Returns list of formatted lines ready to append to actions.
    """
    if not code.strip():
        return []

    lines = code.split('\n')
    result = []

    # For audit purposes, include everything (no truncation)
    for line in lines:
        result.append(f"{prefix}{line}")

    return result


def extract_intermediate_actions(bubble: Dict[str, Any]) -> List[str]:
    """Extract intermediate actions from toolFormerData with detailed information."""
    actions = []

    # Get tool former data which contains the agent's actions
    tool_former_data = bubble.get('toolFormerData', {})

    if isinstance(tool_former_data, dict):
        tool_name = tool_former_data.get('name', '')

        # Parse tool arguments - handle None, empty string, or JSON string
        raw_args = tool_former_data.get('rawArgs')
        args = {}
        if raw_args:
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError):
                args = {}
        
        # Fallback: try to parse params field if rawArgs is missing
        if not args and tool_former_data.get('params'):
            try:
                params_str = tool_former_data.get('params', '')
                if isinstance(params_str, str):
                    params_data = json.loads(params_str)
                    # Extract args from params if available
                    if isinstance(params_data, dict):
                        args = params_data
            except (json.JSONDecodeError, TypeError):
                pass

        # Parse tool results
        result_data = {}
        raw_result = tool_former_data.get('result', '')
        try:
            result_data = json.loads(raw_result) if raw_result else {}
        except json.JSONDecodeError:
            result_data = {}

        # Format action based on tool type with enhanced details
        if tool_name == 'codebase_search':
            query = args.get('query', '')
            target_dirs = args.get('target_directories', [])

            actions.append(f"🔍 Searched: {query}")
            if target_dirs:
                dirs_str = ', '.join(str(d) for d in target_dirs)
                actions.append(f"   → Scope: {dirs_str}")

            # Add file results with line ranges - all results for audit completeness
            code_results = result_data.get('codeResults', [])
            if code_results:
                actions.append(f"   → Found {len(code_results)} result(s)")
                for code_result in code_results:
                    if isinstance(code_result, dict) and 'codeBlock' in code_result:
                        code_block = code_result['codeBlock']
                        file_path = code_block.get('relativeWorkspacePath', '')
                        start_line = code_block.get('startLine', '')
                        end_line = code_block.get('endLine', '')
                        if file_path:
                            if start_line and end_line:
                                actions.append(f"   📁 {file_path}:{start_line}-{end_line}")
                            else:
                                actions.append(f"   📁 {file_path}")

        elif tool_name == 'read_file':
            file_path = args.get('target_file', '')
            offset = args.get('offset')
            limit = args.get('limit')

            if file_path:
                action_str = f"📖 Read file: {file_path}"
                if offset is not None or limit is not None:
                    action_str += f" (lines {offset or 1}"
                    if limit:
                        action_str += f"-{(offset or 1) + limit - 1}"
                    action_str += ")"
                actions.append(action_str)
            else:
                # Tool called but args missing (likely incomplete/cancelled)
                actions.append(f"📖 Read file: (args unavailable)")

        elif tool_name == 'write':
            file_path = args.get('file_path', '')
            contents = args.get('contents', '')
            line_count = len(contents.split('\n')) if contents else 0

            if file_path:
                actions.append(f"✏️ Write file: {file_path}")
                if line_count > 0:
                    actions.append(f"   → {line_count} lines written")

                    # Include full file contents for audit completeness
                    actions.append("   Contents:")
                    actions.extend(format_code_block(contents, prefix='   | '))
            else:
                actions.append(f"✏️ Write file: (args unavailable)")

        elif tool_name == 'search_replace':
            file_path = args.get('file_path', '')
            old_string = args.get('old_string', '')
            new_string = args.get('new_string', '')
            replace_all = args.get('replace_all', False)

            if file_path:
                actions.append(f"🔧 Edit file: {file_path}")
            else:
                actions.append(f"🔧 Edit file: (args unavailable)")

            # Include the actual diff context from the database
            # This makes the content grep-able without losing information
            if file_path and (old_string or new_string):
                # Show what was removed
                if old_string.strip():
                    actions.append("   Old:")
                    actions.extend(format_code_block(
                        old_string, prefix='   - ', max_lines=30))

                # Show what was added
                if new_string.strip():
                    actions.append("   New:")
                    actions.extend(format_code_block(
                        new_string, prefix='   + ', max_lines=30))

                # Add summary
                old_line_count = len(old_string.split('\n')
                                     ) if old_string else 0
                new_line_count = len(new_string.split('\n')
                                     ) if new_string else 0
                net_change = new_line_count - old_line_count

                if net_change > 0:
                    actions.append(f"   → Net change: +{net_change} line(s)")
                elif net_change < 0:
                    actions.append(f"   → Net change: {net_change} line(s)")

            if replace_all:
                actions.append("   → Replace all occurrences")

        elif tool_name == 'run_terminal_cmd':
            command = args.get('command', '')
            is_background = args.get('is_background', False)

            if command:
                action_str = f"💻 Run: `{command}`"
                if is_background:
                    action_str += " (background)"
                actions.append(action_str)
            else:
                # Tool called but args missing (likely incomplete/cancelled)
                actions.append(f"💻 Run: (args unavailable)")

            # Include full command output for grep-ability
            result = result_data.get('output', '')
            if result and isinstance(result, str):
                result_stripped = result.strip()
                if result_stripped:
                    actions.append("   Output:")
                    actions.extend(format_code_block(
                        result_stripped, prefix='   | ', max_lines=100))

            # Also capture exit code if available
            exit_code = result_data.get('exitCode')
            if exit_code is not None and exit_code != 0:
                actions.append(f"   → Exit code: {exit_code}")

        elif tool_name == 'grep':
            pattern = args.get('pattern', '')
            path = args.get('path', '')
            output_mode = args.get('output_mode', 'content')
            glob = args.get('glob')
            case_insensitive = args.get('-i', False)

            grep_opts = []
            if case_insensitive:
                grep_opts.append('-i')
            if glob:
                grep_opts.append(f'--glob {glob}')

            opts_str = ' '.join(grep_opts)
            if opts_str:
                opts_str = f' ({opts_str})'

            if path:
                actions.append(f"🔎 Grep{opts_str}: '{pattern}' in {path}")
            else:
                actions.append(f"🔎 Grep{opts_str}: '{pattern}'")

            # Show result summary
            if output_mode == 'files_with_matches':
                files = result_data.get('files', [])
                if files:
                    actions.append(f"   → Found in {len(files)} file(s)")
            elif output_mode == 'count':
                actions.append("   → Output: match counts")

        elif tool_name == 'list_dir':
            dir_path = args.get('target_directory', '')
            ignore_globs = args.get('ignore_globs', [])

            actions.append(f"📂 Listed directory: {dir_path}")
            if ignore_globs:
                actions.append(f"   → Ignoring: {', '.join(ignore_globs)}")

        elif tool_name == 'glob_file_search':
            pattern = args.get('glob_pattern', '')
            target_dir = args.get('target_directory', '')

            actions.append(f"🔍 File search: {pattern}")
            if target_dir:
                actions.append(f"   → In: {target_dir}")

        elif tool_name == 'delete_file':
            file_path = args.get('target_file', '')
            actions.append(f"🗑️ Delete file: {file_path}")

        elif tool_name == 'edit_notebook':
            notebook = args.get('target_notebook', '')
            cell_idx = args.get('cell_idx', '')
            is_new = args.get('is_new_cell', False)

            action_str = f"📓 Edit notebook: {notebook}"
            if is_new:
                action_str += f" (new cell at {cell_idx})"
            else:
                action_str += f" (cell {cell_idx})"
            actions.append(action_str)

        elif tool_name == 'todo_write':
            todos = args.get('todos', [])
            merge = args.get('merge', False)

            action_str = f"📝 TODO: {'Update' if merge else 'Create'} {len(todos)} item(s)"
            actions.append(action_str)

            # Show ALL todo items
            for todo in todos:
                if isinstance(todo, dict):
                    status = todo.get('status', 'unknown')
                    content = todo.get('content', '')
                    actions.append(f"   - [{status}] {content}")

        elif tool_name == 'web_search':
            search_term = args.get('search_term', '')
            actions.append(f"🌐 Web search: {search_term}")

            # Web search results are in the tool result, not bubble-level
            if result_data and isinstance(result_data, dict):
                references = result_data.get('references', [])
                if references:
                    actions.append(f"   → {len(references)} result(s)")
                    for i, ref in enumerate(references, 1):
                        if isinstance(ref, dict):
                            title = ref.get('title', 'Untitled')
                            chunk = ref.get('chunk', '')
                            actions.append(f"   {i}. {title}")
                            if chunk:
                                # Include the full chunk content for audit completeness
                                actions.append(f"      {chunk}")
                elif result_data.get('rejected'):
                    actions.append(f"   → Search was rejected/cancelled")

        elif tool_name and tool_name != '':
            # Generic tool with name
            actions.append(f"🔧 {tool_name}")
            # Show ALL key arguments
            if args:
                key_args = []
                for key in ['file_path', 'target_file', 'path', 'query', 'pattern']:
                    if key in args and args[key]:
                        key_args.append(f"{key}={str(args[key])}")
                if key_args:
                    actions.append(f"   → {', '.join(key_args)}")

    # Check for bubble-level web search results (legacy/alternative storage)
    # Note: Most web search results are now in the tool's result field (handled above)
    web_search = bubble.get('aiWebSearchResults', [])
    if web_search:
        actions.append(f"🌐 Additional web search results: {len(web_search)} result(s)")
        for i, result in enumerate(web_search, 1):
            if isinstance(result, dict):
                title = result.get('title', 'Untitled')
                url = result.get('url', '')
                snippet = result.get('snippet', '')
                chunk = result.get('chunk', '')
                if title or url:
                    actions.append(f"   {i}. {title}")
                    if url:
                        actions.append(f"      URL: {url}")
                    if snippet:
                        actions.append(f"      Snippet: {snippet}")
                    if chunk:
                        actions.append(f"      Content: {chunk}")

    # Check for docs references - include ALL
    docs_refs = bubble.get('docsReferences', [])
    if docs_refs:
        actions.append(f"📚 Docs: {len(docs_refs)} reference(s)")
        for ref in docs_refs:
            if isinstance(ref, dict):
                title = ref.get('title', '')
                url = ref.get('url', '')
                if title:
                    actions.append(f"   - {title}")
                if url:
                    actions.append(f"     {url}")

    # Check for web references - include ALL
    web_refs = bubble.get('webReferences', [])
    if web_refs:
        for ref in web_refs:
            if isinstance(ref, dict) and ref.get('url'):
                actions.append(f"🔗 {ref['url']}")

    # Check for context pieces
    context_pieces = bubble.get('contextPieces', [])
    if context_pieces:
        actions.append(f"📋 Context: {len(context_pieces)} piece(s)")

    return actions


def get_message_role(bubble: Dict[str, Any]) -> str:
    """Determine message role from bubble data."""
    # Check type field first (most reliable)
    bubble_type = bubble.get('type')
    if isinstance(bubble_type, int):
        if bubble_type == 1:
            return 'user'
        elif bubble_type == 2:
            return 'assistant'

    # Fallback to role/authorRole fields
    role = bubble.get('role') or bubble.get(
        'authorRole') or bubble.get('sender')
    if isinstance(role, str):
        role_lower = role.lower()
        if role_lower in ('user', 'human', 'client'):
            return 'user'
        elif role_lower in ('assistant', 'ai', 'agent', 'bot', 'model'):
            return 'assistant'

    return 'unknown'


def format_timestamp_filename(timestamp_ms: int) -> str:
    """Format timestamp for filename: YYYY-MM-DDTHHMM."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000)
    return dt.strftime('%Y-%m-%dT%H%M')


def slugify_title(title: str, max_length: int = 50) -> str:
    """Convert title to URL-safe slug."""
    import re

    # Take first line only and limit length
    title = title.split('\n')[0][:max_length]

    # Replace problematic characters
    replacements = {
        '/': '-', '\\': '-', ':': '-', '*': '-', '?': '-',
        '"': '', '<': '-', '>': '-', '|': '-', ' ': '-'
    }

    for old, new in replacements.items():
        title = title.replace(old, new)

    # Clean up multiple dashes and trim
    title = re.sub(r'-+', '-', title).strip('-')

    return title or 'untitled'


def generate_filename(thread_data: Dict[str, Any], cid: str, first_message: str) -> str:
    """Generate filename for conversation export."""
    # Get timestamp
    created_at = thread_data.get('createdAt', 0)
    timestamp_str = format_timestamp_filename(created_at)

    # Get title (prefer thread name, fallback to first message)
    title = thread_data.get('name') or first_message
    title_slug = slugify_title(title)

    # Generate short hash
    cid_short = cid[:8]

    return f"{timestamp_str}-{title_slug}-{cid_short}.md"


def format_conversation_markdown(thread_data: Dict[str, Any], cid: str, bubbles: List[Dict[str, Any]], filename: str, conn: sqlite3.Connection) -> str:
    """Format conversation as markdown."""
    # Extract metadata
    thread_name = thread_data.get('name', 'Untitled Conversation')
    created_at = thread_data.get('createdAt', 0)
    updated_at = thread_data.get('lastUpdatedAt', created_at)

    created_str = datetime.fromtimestamp(
        created_at / 1000).strftime('%Y-%m-%d %H:%M:%S')
    updated_str = datetime.fromtimestamp(
        updated_at / 1000).strftime('%Y-%m-%d %H:%M:%S')

    # Calculate total tokens if available
    total_input_tokens = 0
    total_output_tokens = 0
    for bubble in bubbles:
        token_count = bubble.get('tokenCount', {})
        if isinstance(token_count, dict):
            total_input_tokens += token_count.get('inputTokens', 0)
            total_output_tokens += token_count.get('outputTokens', 0)

    # Build markdown
    md_lines = [
        f"# {thread_name}",
        "",
        f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Thread ID:** `{cid}`  ",
        f"**Created:** {created_str}  ",
        f"**Last Updated:** {updated_str}  ",
        f"**Messages:** {len(bubbles)}  ",
    ]

    # Add token usage if available
    if total_input_tokens > 0 or total_output_tokens > 0:
        total_tokens = total_input_tokens + total_output_tokens
        md_lines.append(
            f"**Tokens:** {total_tokens:,} ({total_input_tokens:,} in / {total_output_tokens:,} out)  ")

    md_lines.append("")

    # Add context info if available
    context = thread_data.get('context')
    if context:
        context_files = context.get(
            'files', []) if isinstance(context, dict) else []
        if context_files:
            file_list = ', '.join(f['name'] for f in context_files)
            md_lines.extend([
                f"**Context:** {file_list}  ",
                ""
            ])

    # Process messages - group consecutive agent content together
    i = 0
    last_role = None

    while i < len(bubbles):
        bubble = bubbles[i]
        content = extract_message_content(bubble)
        role = get_message_role(bubble)
        bubble_id = bubble.get('bubbleId', '')

        # Get message context data
        context_data = get_message_context(
            conn, cid, bubble_id) if bubble_id else {}

        # Check for thinking content
        thinking_content = extract_thinking_content(bubble)

        # Check for intermediate actions (agent context)
        actions = extract_intermediate_actions(bubble)

        # Check for context information
        context_info = []
        if context_data:
            # Files used - include ALL for audit completeness
            files = context_data.get('files', [])
            if files:
                context_info.append(f"**Files Referenced ({len(files)}):**")
                for file_info in files:
                    if isinstance(file_info, dict) and 'path' in file_info:
                        context_info.append(f"- `{file_info['path']}`")
                context_info.append("")

            # TODOs - include ALL
            todos = context_data.get('todos', [])
            if todos:
                context_info.append(f"**[TODOs]** ({len(todos)})")
                for todo in todos:
                    if isinstance(todo, dict):
                        todo_content = todo.get('content', 'Unknown')
                        todo_status = todo.get('status', 'unknown')
                        context_info.append(
                            f"- [{todo_status}] {todo_content}")
                    elif isinstance(todo, str):
                        # If it's a string, try to parse as JSON
                        try:
                            todo_obj = json.loads(todo)
                            if isinstance(todo_obj, dict):
                                todo_content = todo_obj.get(
                                    'content', 'Unknown')
                                todo_status = todo_obj.get('status', 'unknown')
                                context_info.append(
                                    f"- [{todo_status}] {todo_content}")
                            else:
                                context_info.append(f"- {todo}")
                        except:
                            context_info.append(f"- {todo}")
                    else:
                        context_info.append(f"- {todo}")
                context_info.append("")

            # Knowledge items (cursor rules) - skip these

            # Terminal files - include ALL
            terminal_files = context_data.get('terminalFiles', [])
            if terminal_files:
                context_info.append(
                    f"**Terminal Context ({len(terminal_files)}):**")
                for term_file in terminal_files:
                    context_info.append(f"- {term_file}")
                context_info.append("")

            # Cursor rules - show ALL with complete content
            cursor_rules = context_data.get('cursorRules', [])
            if cursor_rules:
                context_info.append(f"**Cursor Rules ({len(cursor_rules)}):**")
                for rule in cursor_rules:
                    if isinstance(rule, dict):
                        rule_content = rule.get(
                            'content', rule.get('text', ''))
                        if rule_content:
                            context_info.append(f"- {rule_content}")
                    elif isinstance(rule, str):
                        context_info.append(f"- {rule}")
                context_info.append("")

        # Add git-related information from bubble - ALL diffs with COMPLETE content
        git_diffs = bubble.get('gitDiffs', [])
        if git_diffs:
            context_info.append(f"**Git Diffs ({len(git_diffs)}):**")
            for diff in git_diffs:
                if isinstance(diff, dict):
                    file_path = diff.get('path', 'Unknown')
                    diff_content = diff.get('diff', '')
                    context_info.append(f"- {file_path}")
                    if diff_content:
                        # Include the COMPLETE diff for audit purposes
                        context_info.append("  ```diff")
                        diff_lines = diff_content.split('\n')
                        for line in diff_lines:
                            context_info.append(f"  {line}")
                        context_info.append("  ```")
            context_info.append("")

        commits = bubble.get('commits', [])
        if commits:
            context_info.append(f"**Git Commits ({len(commits)}):**")
            for commit in commits:
                if isinstance(commit, dict):
                    sha = commit.get('sha', '')[:8]
                    message = commit.get('message', 'No message')
                    context_info.append(f"- {sha}: {message}")
            context_info.append("")

        pull_requests = bubble.get('pullRequests', [])
        if pull_requests:
            context_info.append(f"**Pull Requests ({len(pull_requests)}):**")
            for pr in pull_requests:
                if isinstance(pr, dict):
                    number = pr.get('number', '')
                    title = pr.get('title', 'No title')
                    context_info.append(f"- PR #{number}: {title}")
            context_info.append("")

        # Add linting errors - ALL errors with complete messages
        lints = bubble.get('lints', [])
        approx_lints = bubble.get('approximateLintErrors', [])
        multi_lints = bubble.get('multiFileLinterErrors', [])

        all_lints = lints + approx_lints + multi_lints
        if all_lints:
            context_info.append(f"**Linting Issues ({len(all_lints)}):**")
            for lint in all_lints:
                if isinstance(lint, dict):
                    severity = lint.get('severity', 'error')
                    message = lint.get('message', 'Unknown')
                    file_path = lint.get('file', '')
                    line = lint.get('line', '')

                    lint_str = f"- [{severity}] {message}"
                    if file_path:
                        lint_str += f" ({file_path}"
                        if line:
                            lint_str += f":{line}"
                        lint_str += ")"
                    context_info.append(lint_str)
            context_info.append("")

        # Add human edits to AI suggestions - ALL changes
        human_changes = bubble.get('humanChanges', [])
        if human_changes:
            context_info.append(f"**Human Edits ({len(human_changes)}):**")
            for change in human_changes:
                if isinstance(change, dict):
                    file_path = change.get('file', 'Unknown')
                    change_type = change.get('type', 'edit')
                    context_info.append(f"- {change_type}: {file_path}")
            context_info.append("")

        # Add attached folders context - ALL folders
        attached_folders = bubble.get(
            'attachedFolders', []) or bubble.get('attachedFoldersNew', [])
        if attached_folders:
            context_info.append(
                f"**Attached Folders ({len(attached_folders)}):**")
            for folder in attached_folders:
                if isinstance(folder, dict):
                    folder_path = folder.get(
                        'path', folder.get('name', 'Unknown'))
                    context_info.append(f"- {folder_path}")
                elif isinstance(folder, str):
                    context_info.append(f"- {folder}")
            context_info.append("")

        # Add recently viewed files - ALL files
        recently_viewed = bubble.get('recentlyViewedFiles', [])
        if recently_viewed and len(recently_viewed) > 0:
            context_info.append(
                f"**Recently Viewed Files ({len(recently_viewed)}):**")
            for file in recently_viewed:
                if isinstance(file, dict):
                    file_path = file.get('path', file.get('file', ''))
                    if file_path:
                        context_info.append(f"- {file_path}")
                elif isinstance(file, str):
                    context_info.append(f"- {file}")
            context_info.append("")

        # Add images - ALL images
        images = bubble.get('images', [])
        if images:
            context_info.append(f"**Images ({len(images)}):**")
            for img in images:
                if isinstance(img, dict):
                    img_name = img.get('name', img.get('filename', 'Unnamed'))
                    img_type = img.get('type', img.get('mimeType', 'unknown'))
                    context_info.append(f"- {img_name} ({img_type})")
                elif isinstance(img, str):
                    context_info.append(f"- {img}")
            context_info.append("")

        # Add capabilities/mode if interesting
        is_agentic = bubble.get('isAgentic')
        if is_agentic:
            context_info.append("**Mode:** Agentic")
            context_info.append("")

        # Skip empty bubbles unless they have actions, context, or thinking
        if not content.strip() and not actions and not context_info and not thinking_content:
            i += 1
            continue

        # Add separator only when transitioning between user and agent
        if last_role is not None and last_role != role:
            md_lines.extend(["", "---", ""])

        # Check if this is an action-only bubble and look ahead for consecutive action bubbles
        if not content.strip() and actions and not context_info and not thinking_content:
            # This is an action-only bubble - collect all consecutive action bubbles
            all_actions = []
            all_actions.extend(actions)

            # Look ahead for more action-only bubbles
            j = i + 1
            while j < len(bubbles):
                next_bubble = bubbles[j]
                next_content = extract_message_content(next_bubble)
                next_bubble_id = next_bubble.get('bubbleId', '')
                next_context_data = get_message_context(
                    conn, cid, next_bubble_id) if next_bubble_id else {}
                next_actions = extract_intermediate_actions(next_bubble)
                next_thinking = extract_thinking_content(next_bubble)

                # Check if next bubble is also action-only (no content, no context, no thinking, but has actions)
                next_context_info = []
                if next_context_data:
                    for field in ['files', 'todos', 'terminalFiles', 'cursorRules']:
                        if next_context_data.get(field):
                            next_context_info.append(field)

                if not next_content.strip() and next_actions and not next_context_info and not next_thinking:
                    all_actions.extend(next_actions)
                    j += 1
                else:
                    break

            # Output grouped actions
            md_lines.append("**[Actions]**")
            for action in all_actions:
                md_lines.append(f"- {action}")
            md_lines.append("")  # Just one blank line after actions

            # Skip to the next non-action bubble
            i = j
            last_role = 'assistant'  # Actions are from assistant
            continue

        # Format thinking content if available (before main content)
        if thinking_content:
            md_lines.append(f"**[Thinking]** {thinking_content}")
            md_lines.append("")

        # Format content if available
        if content.strip():
            if role == 'user':
                md_lines.append(f"**[User]** {content}")
            elif role == 'assistant':
                md_lines.append(f"**[Agent]** {content}")
            else:
                md_lines.append(f"**[{role.title()}]** {content}")

        # Add context information if present
        if context_info:
            if content.strip():
                md_lines.append("")  # Add space between content and context
            md_lines.extend(context_info)

        # Add intermediate actions if present (for bubbles with content)
        if actions and content.strip():
            md_lines.append("")  # Add space before actions
            md_lines.append("**[Actions]**")
            for action in actions:
                md_lines.append(f"- {action}")

        # Add blank line after this bubble content (but not separator yet)
        md_lines.append("")

        last_role = role
        i += 1

    return '\n'.join(md_lines)


def export_conversations(output_dir: str, verbose: bool = False, min_timestamp_ms: int = None) -> None:
    """Export all Cursor conversations to markdown files.
    
    Args:
        output_dir: Directory to export markdown files to
        verbose: Enable verbose output
        min_timestamp_ms: Optional minimum timestamp (milliseconds) to filter conversations
    """
    # Setup
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if verbose:
        if min_timestamp_ms:
            min_dt = datetime.fromtimestamp(min_timestamp_ms / 1000)
            print(f"Filtering conversations created after: {min_dt}")

    # Get all threads from global and workspace databases
    threads = get_all_composer_threads(verbose)
    
    # Filter by timestamp if provided
    if min_timestamp_ms:
        threads = [(cid, data) for cid, data in threads 
                  if data.get('createdAt', 0) >= min_timestamp_ms]
    
    if verbose:
        print(f"Processing {len(threads)} conversation threads")

    # Open a connection to global DB for getting bubbles (they're still there)
    global_db = get_global_cursor_db_path()
    if not os.path.exists(global_db):
        print(f"Error: Cursor global database not found at {global_db}")
        return
    
    conn = connect_db_readonly(global_db)

    try:

        if verbose:
            print(f"Found {len(threads)} conversation threads")

        # Pre-index existing files by cid short-hash (last 8 chars of stem before .md)
        # Filename format: {timestamp}-{slug}-{cid[:8]}.md
        existing_by_cid: Dict[str, Path] = {}
        for f in output_path.glob('*.md'):
            existing_by_cid[f.stem[-8:]] = f

        exported_count = 0

        # Partition threads into up-to-date (skip) vs. needs-export, then
        # batch-fetch all bubble data for the latter in a single table scan.
        needs_export = []
        for cid, thread_data in threads:
            cid_short = cid[:8]
            thread_updated_ms = thread_data.get('lastUpdatedAt', thread_data.get('createdAt', 0))
            if thread_updated_ms and cid_short in existing_by_cid:
                if existing_by_cid[cid_short].stat().st_mtime * 1000 >= thread_updated_ms:
                    exported_count += 1
                    continue
            needs_export.append((cid, thread_data))

        bubbles_by_cid = get_bubbles_batch(conn, {cid for cid, _ in needs_export})

        for cid, thread_data in needs_export:
            bubbles = bubbles_by_cid.get(cid, [])

            if not bubbles:
                if verbose:
                    print(f"Skipping empty thread: {cid}")
                continue

            # Get first message for filename
            first_content = ""
            for bubble in bubbles:
                content = extract_message_content(bubble)
                if content.strip():
                    first_content = content
                    break

            if not first_content:
                if verbose:
                    print(f"Skipping thread with no content: {cid}")
                continue

            # Generate filename and content
            filename = generate_filename(thread_data, cid, first_content)
            file_path = output_path / filename

            markdown_content = format_conversation_markdown(
                thread_data, cid, bubbles, filename, conn)

            # Write file
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(markdown_content)

                existing_by_cid[cid_short] = file_path
                exported_count += 1

                if verbose:
                    thread_name = thread_data.get('name', 'Untitled')
                    print(f"Exported: {thread_name} -> {filename}")

            except Exception as e:
                print(f"Error writing {filename}: {e}")

        # Export terminal history as a separate file
        export_terminal_history(output_path, conn, verbose)

        print(f"Export complete! Exported {exported_count} conversations to: {output_path}")

    finally:
        conn.close()


def generate_file_modification_timeline(output_path: Path, conn: sqlite3.Connection, verbose: bool = False) -> None:
    """Generate a timeline of file modifications across all conversations."""
    try:
        # Track file modifications: {file_path: [(timestamp, cid, thread_name, action, details)]}
        file_timeline = {}

        threads = get_composer_threads(conn)

        for cid, thread_data in threads:
            thread_name = thread_data.get('name', 'Untitled')
            created_at = thread_data.get('createdAt', 0)
            timestamp_str = datetime.fromtimestamp(
                created_at / 1000).strftime('%Y-%m-%d %H:%M')

            bubbles = get_thread_bubbles(conn, cid)

            for bubble in bubbles:
                tool_former_data = bubble.get('toolFormerData', {})
                if not isinstance(tool_former_data, dict):
                    continue

                tool_name = tool_former_data.get('name', '')

                # Parse arguments
                raw_args = tool_former_data.get('rawArgs', '')
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    continue

                # Track file modifications
                file_path = None
                action = None
                details = []

                if tool_name == 'write':
                    file_path = args.get('file_path', '')
                    action = 'Created/Overwritten'
                    contents = args.get('contents', '')
                    line_count = len(contents.split('\n')) if contents else 0
                    details.append(f"{line_count} lines")

                elif tool_name == 'search_replace':
                    file_path = args.get('file_path', '')
                    action = 'Modified'
                    old_string = args.get('old_string', '')
                    new_string = args.get('new_string', '')

                    # Capture key change
                    if old_string and new_string:
                        old_lines = old_string.split('\n')
                        new_lines = new_string.split('\n')

                        if not old_string.strip():
                            preview = new_string.strip()
                            details.append(f"Added: {preview}")
                        elif not new_string.strip():
                            preview = old_string.strip()
                            details.append(f"Removed: {preview}")
                        else:
                            # Find first changed line
                            for old_line, new_line in zip(old_lines, new_lines):
                                if old_line != new_line:
                                    if 'require' in new_line or 'import' in new_line:
                                        details.append(new_line.strip())
                                    elif 'def ' in new_line or 'class ' in new_line:
                                        details.append(new_line.strip())
                                    else:
                                        details.append(f"Changed line")
                                    break

                            if len(new_lines) > len(old_lines):
                                details.append(
                                    f"+{len(new_lines) - len(old_lines)} lines")
                            elif len(old_lines) > len(new_lines):
                                details.append(
                                    f"-{len(old_lines) - len(new_lines)} lines")

                elif tool_name == 'delete_file':
                    file_path = args.get('target_file', '')
                    action = 'Deleted'

                elif tool_name == 'edit_notebook':
                    file_path = args.get('target_notebook', '')
                    action = 'Notebook Edit'
                    cell_idx = args.get('cell_idx', '')
                    is_new = args.get('is_new_cell', False)
                    if is_new:
                        details.append(f"New cell {cell_idx}")
                    else:
                        details.append(f"Cell {cell_idx}")

                # Record modification
                if file_path and action:
                    if file_path not in file_timeline:
                        file_timeline[file_path] = []

                    file_timeline[file_path].append({
                        'timestamp': created_at,
                        'timestamp_str': timestamp_str,
                        'cid': cid[:8],
                        'thread_name': thread_name,
                        'action': action,
                        'details': details
                    })

        # Generate timeline markdown
        md_lines = [
            "# File Modification Timeline",
            "",
            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
            f"**Total Files Modified:** {len(file_timeline)}  ",
            "",
            "This timeline tracks all file modifications made across Cursor conversations.",
            "Files are listed in order of most recently modified first.",
            "",
            "---",
            ""
        ]

        # Sort files by most recent modification
        sorted_files = sorted(
            file_timeline.items(),
            key=lambda x: max(m['timestamp'] for m in x[1]),
            reverse=True
        )

        for file_path, modifications in sorted_files:
            # Sort modifications chronologically
            sorted_mods = sorted(modifications, key=lambda x: x['timestamp'])

            md_lines.append(f"## `{file_path}`")
            md_lines.append("")
            md_lines.append(f"**Total modifications:** {len(modifications)}")
            md_lines.append("")

            for mod in sorted_mods:
                detail_str = ' — '.join(
                    mod['details']) if mod['details'] else ''
                if detail_str:
                    detail_str = f": {detail_str}"

                md_lines.append(
                    f"- **{mod['timestamp_str']}** — {mod['action']}{detail_str}  "
                    f"  *[{mod['thread_name']}]* `({mod['cid']})`"
                )

            md_lines.append("")
            md_lines.append("---")
            md_lines.append("")

        # Write timeline file
        timeline_file = output_path / "file-modification-timeline.md"
        with open(timeline_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))

        if verbose:
            print(f"Generated file modification timeline: {len(file_timeline)} files tracked")

    except Exception as e:
        if verbose:
            print(f"Error generating file modification timeline: {e}")


def export_terminal_history(output_path: Path, conn: sqlite3.Connection, verbose: bool = False) -> None:
    """Export terminal command history to a separate file."""
    try:
        # Connect to ItemTable to get terminal history
        item_conn = connect_db_readonly(get_global_cursor_db_path())
        cursor = item_conn.cursor()

        # Get terminal commands
        cursor.execute(
            "SELECT value FROM ItemTable WHERE key = 'terminal.history.entries.commands'")
        commands_result = cursor.fetchone()

        # Get terminal directories
        cursor.execute(
            "SELECT value FROM ItemTable WHERE key = 'terminal.history.entries.dirs'")
        dirs_result = cursor.fetchone()

        if commands_result and commands_result[0]:
            commands_data = json.loads(commands_result[0])
            commands = commands_data.get('entries', [])

            # Generate terminal history markdown
            md_lines = [
                "# Terminal History",
                "",
                f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
                f"**Total Commands:** {len(commands)}  ",
                "",
                "## Recent Commands",
                ""
            ]

            # Add commands (showing most recent first)
            # Last 50 commands
            for i, cmd_entry in enumerate(reversed(commands[-50:])):
                if isinstance(cmd_entry, dict):
                    command = cmd_entry.get('key', '')
                    shell_info = cmd_entry.get('value', {})
                    shell_type = shell_info.get('shellType', 'unknown') if isinstance(
                        shell_info, dict) else 'unknown'

                    # Clean up multi-line commands for better formatting
                    if '\\n' in command:
                        # Multi-line command - format nicely
                        clean_cmd = command.replace('\\n', ' \\\\\n    ')
                        md_lines.append(
                            f"### Command {len(commands)-len(commands[-50:])+i+1} ({shell_type})")
                        md_lines.append("```bash")
                        md_lines.append(clean_cmd)
                        md_lines.append("```")
                    else:
                        # Single line command
                        md_lines.append(
                            f"**{len(commands)-len(commands[-50:])+i+1}.** `{command}` ({shell_type})")
                    md_lines.append("")

            # Add directory history if available
            if dirs_result and dirs_result[0]:
                dirs_data = json.loads(dirs_result[0])
                dirs = dirs_data.get('entries', [])

                if dirs:
                    md_lines.extend([
                        "## Recent Directories",
                        ""
                    ])

                    # Last 20 directories
                    for i, dir_entry in enumerate(reversed(dirs[-20:])):
                        if isinstance(dir_entry, dict):
                            directory = dir_entry.get('key', '')
                            md_lines.append(f"**{i+1}.** `{directory}`")
                    md_lines.append("")

            # Write terminal history file
            terminal_file = output_path / "terminal-history.md"
            with open(terminal_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(md_lines))

            if verbose:
                print(
                    f"Exported terminal history: terminal-history.md ({len(commands)} commands)")

        item_conn.close()

    except Exception as e:
        if verbose:
            print(f"Error exporting terminal history: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Export Cursor chat conversations to markdown files')
    parser.add_argument(
        'output_dir', help='Directory to export markdown files to')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose output')
    parser.add_argument('--min-timestamp-ms', type=int, default=None,
                        help='Minimum timestamp (milliseconds) to filter conversations')

    args = parser.parse_args()

    export_conversations(args.output_dir, args.verbose, args.min_timestamp_ms)


if __name__ == '__main__':
    import time as _time

    _lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("export-cursor: another instance is already running, exiting")
        sys.exit(0)

    _start = _time.time()
    _start_iso = _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime(_start))
    main()
    with open(os.path.expanduser('~/log/cron.log'), 'a') as _f:
        _f.write(f'{_start_iso}\t{int(_time.time() - _start)}\texport-cursor.py\n')
