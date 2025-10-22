# Cursor Database Schema Documentation

## Overview

Cursor stores workspace data in SQLite databases located at:
`~/Library/Application Support/Cursor/User/workspaceStorage/{workspaceHash}/state.vscdb`

## Database Tables (2)

### Table: ItemTable

**Schema:**
| Column | Type | Nullable | Default | Primary Key |
|--------|------|----------|---------|-------------|
| key | TEXT | Yes | NULL | No |
| value | BLOB | Yes | NULL | No |

**Row Count:** 158

**Key Analysis:**
- **workbench**: 122 keys (UI state, panels, chat views)
  - `workbench.activityBar.hidden`
  - `workbench.agentMode.exitInfo`
  - `workbench.auxiliaryBar.hidden`
  - `workbench.panel.composerChatViewPane.{id}` (chat panel states)
  - `workbench.panel.aichat.{id}.numberOfVisibleViews` (AI chat views)

- **aiService**: 2 keys (AI interaction data)
  - `aiService.generations` (AI responses with timestamps)
  - `aiService.prompts` (User prompts without timestamps)

- **composer**: 1 key (Chat tab metadata)
  - `composer.composerData` (Chat sessions with names, IDs, timestamps)

- **interactive**: 1 key (Conversation sessions)
  - `interactive.sessions` (Potentially structured conversation data)

- **history**: 1 key (Historical data)
  - `history.entries` (45 items - could be conversation history)

- **memento/workbench**: 6 keys (Editor state)
- **terminal**: 3 keys (Terminal state)
- **[other prefixes]**: Various extension and UI state data

### Table: cursorDiskKV

**Schema:**
| Column | Type | Nullable | Default | Primary Key |
|--------|------|----------|---------|-------------|
| key | TEXT | Yes | NULL | No |
| value | BLOB | Yes | NULL | No |

**Row Count:** 0 (Empty)

## Key Tables for Chat Export

Based on analysis, the following tables/keys are most relevant for chat export:

1. **`composer.composerData`** - Contains chat tab metadata (names, IDs, timestamps)
2. **`aiService.generations`** - Contains AI responses with timestamps  
3. **`aiService.prompts`** - Contains user prompts (no timestamps)
4. **`interactive.sessions`** - Potentially contains conversation structure
5. **`history.entries`** - May contain conversation history
6. **`workbench.panel.composerChatViewPane.{id}`** - Chat panel UI state
7. **`workbench.panel.aichat.view.{id}`** - AI chat view references

## Chat Tabs Identification ✅

**Found: `composer.composerData`** contains the chat tab metadata visible in the UI.

**Structure:**
```json
{
  "allComposers": [
    {
      "name": "Containerize sweetness",
      "composerId": "3ef6b375-c30b-4594-ad3a-b18936790e69", 
      "createdAt": 1758239853970,
      "lastUpdatedAt": 1758240102372,
      "subtitle": "sweetness-deployment-guide.md, sweetness-docker-compose.md, ...",
      "type": "composer",
      "contextUsagePercent": 36.8,
      "totalLinesAdded": 0,
      "totalLinesRemoved": 0,
      // ... other metadata fields
    }
  ],
  "selectedComposerIds": [...],
  "lastFocusedComposerIds": [...],
  // ... other top-level fields
}
```

**Key Fields:**
- `name`: Chat tab title (matches UI exactly)
- `composerId`: Unique session identifier  
- `createdAt/lastUpdatedAt`: Session timestamps
- `subtitle`: Context files involved
- Additional metadata: usage stats, archive status, etc.

**Verification:** All target chats from UI screenshot found:
✅ "Database followers", "Containerize sweetness", "Brainstorming an llm gateway service", "Review and update cursor-exporter repo"

## Conversation Events Identification ✅

**Found: Conversation data is stored in `aiService.generations` and `aiService.prompts`**

### Key Discovery: Dual Storage Pattern

**User Messages** appear in BOTH tables:
- `aiService.prompts`: Basic text + commandType (no timestamps/IDs)
- `aiService.generations`: Same text + timestamps + UUIDs + type="composer"

**AI Messages** appear ONLY in:
- `aiService.generations`: With timestamps + UUIDs + type="composer"

### Data Structure

**`aiService.generations`** (100 items):
```json
{
  "unixMs": 1758239874059,
  "generationUUID": "e0d7ca53-ff04-41e5-a41b-08f6a3b9d898", 
  "type": "composer",
  "textDescription": "figure out a plan to containerize sweetness..."
}
```

**`aiService.prompts`** (201 items):
```json
{
  "text": "figure out a plan to containerize sweetness...",
  "commandType": 4
}
```

### Critical Issue: Session Timestamp Boundaries Are Wrong!

**Problem**: `composer.composerData` timestamps (`createdAt`/`lastUpdatedAt`) do NOT represent actual conversation boundaries.

**Evidence**: "Containerize sweetness" session:
- Official timeframe: 16:57:33 to 17:01:42 (4 minutes)
- Actual conversation: Continues until 17:53:51+ (56+ minutes)
- The conversation flows into "Database followers" and "Review cursor-exporter" topics

**Session metadata appears to track UI state changes, not conversation flow.**

## Conversation Mapping BREAKTHROUGH! ✅

**DISCOVERED: Request IDs = Generation UUIDs = Conversation Boundaries**

### The Missing Link Found!

Each chat tab visible in the UI has a **request ID** that appears as the `generationUUID` for the **first message** in that conversation.

**Example Mapping:**
```
Chat Tab: "Database followers" 
Request ID: 75cbb749-2b81-49d4-a48e-61354cce569d
→ Found as generationUUID in Gen #86: "looks like your connection hung..."

Chat Tab: "Containerize sweetness"
Request ID: e0d7ca53-ff04-41e5-a41b-08f6a3b9d898  
→ Found as generationUUID in Gen #78: "figure out a plan to containerize sweetness..."
```

### Conversation Boundary Algorithm

1. **Find conversation starts**: Look for generations where `generationUUID` matches a composer session request ID
2. **Group messages**: All generations between consecutive request ID markers belong to the same conversation
3. **Include user messages**: Check if generation text also appears in `aiService.prompts` (indicates user message)
4. **Sort chronologically**: Order messages within each conversation by `unixMs` timestamp

### Proven Structure

```
Gen #78 (Request ID: e0d7ca53...) ← "Containerize sweetness" START
Gen #79-85 ← Belong to "Containerize sweetness"  
Gen #86 (Request ID: 75cbb749...) ← "Database followers" START
Gen #87-93 ← Belong to "Database followers"
Gen #94 (Request ID: 2037eade...) ← "Review cursor-exporter" START  
Gen #95-99 ← Belong to "Review cursor-exporter"
```

**This eliminates ALL timestamp correlation problems!**

## CORRECTED: Complete Solution ✅

**BREAKTHROUGH: Found working solution in global database!**

### Correct Database Location
- **❌ WRONG**: `~/Library/Application Support/Cursor/User/workspaceStorage/{hash}/state.vscdb`
- **✅ CORRECT**: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`

### Correct Data Structure
1. **Chat Tabs**: `cursorDiskKV` table, key `composerData:{cid}` 
2. **Conversation Messages**: `cursorDiskKV` table, key `bubbleId:{cid}:%`
3. **Role Attribution**: `type: 1` = User, `type: 2` = Assistant  
4. **Complete Conversations**: Each thread has both user and AI messages with full content
5. **Rich Metadata**: Each bubble contains extensive context, tool results, thinking blocks

### Verified Working Implementation
- ✅ "Containerize sweetness" thread: 31 complete messages (not just 2 prompts)
- ✅ Full AI responses with detailed technical content  
- ✅ Proper user/assistant role attribution
- ✅ All conversation context preserved

**Previous analysis was based on wrong database - workspace databases only contain UI state, not conversation content!**

## Final Working Solution

**✅ Created:** `export-cursor-corrected.py` - Complete implementation that works!

**Test Results:**
- ✅ Exported 56 complete conversations from global database
- ✅ "Containerize sweetness": 31 messages (vs 2 incomplete messages before)
- ✅ Full user/AI conversations with proper role attribution
- ✅ All AI responses with complete technical content
- ✅ Proper markdown formatting with timestamps and metadata

**Usage:**
```bash
python3 export-cursor-corrected.py /path/to/output --verbose
```

**Key Insights:**
1. **Database Location**: Global storage contains all conversation data
2. **Data Structure**: `cursorDiskKV` table with `composerData:` and `bubbleId:` keys  
3. **Role Attribution**: `type: 1` = User, `type: 2` = Assistant
4. **Complete Conversations**: Each thread has full bidirectional conversation history
5. **Rich Metadata**: Extensive context, timestamps, usage data in each bubble

**This implementation successfully solves the original requirement!**
