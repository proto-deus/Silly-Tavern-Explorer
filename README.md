# Silly Tavern Explorer

A desktop application for browsing, editing, and managing SillyTavern character cards. It provides a visual library for character card PNG files with embedded JSON metadata (V2/V3 spec) and Lorebooks.

## Features

### Application
- **Keyboard shortcuts** — centralized registry (`src/ui/shortcuts.py`) drives all menu actions
- **Unsaved-changes guard** — prompts Save/Discard/Cancel when switching tabs or closing with unsaved edits
- **Full-screen image viewer** — zoomable, pannable dialog with Save As support (Ctrl+=/Ctrl+-/wheel)
- **Status bar** showing card count, total tokens, and favorites count; transient messages for import/export/save
- **Shared character sidebar** — a single card list on the left serves the Edit, Generate, and Test tabs (shared selection, search, and favorites filter)
- **Startup dependency check** — missing packages are reported before Qt loads (native message box on Windows, Qt dialog or terminal message elsewhere)
- **Single-instance lock** — a second launch detects the running instance and exits
- **Database backup** — automatic `.bak`/`.bak2` rotation on startup when the DB has changed, plus a manual `backup()` method (File > Backup Now)
- **Duplicate scanner** — finds cards sharing the same name + creator (case-insensitive), or near-identical images via perceptual hashing; tree view with thumbnails, delete selected, or keep one and delete the rest of a group (View > Find Duplicates)
- **Statistics dashboard** — aggregate metrics rendered as colored bars: total/avg/min/max tokens, top-10 tags, spec version distribution, creator leaderboard, and cards-added-per-week sparkline (View > Statistics)
- **Font Size** — global application font size (8–32px) configurable via View > Font Size, applied app-wide and persisted across sessions
- **Session state restore** — window geometry, selected cards, and library scroll position are saved on exit and restored on the next launch
- **Full-library backup & restore** — File > Backup Library zips the database, all card files, chat sessions, and thumbnails into a single archive with a manifest; File > Restore Library validates and swaps a backup back in atomically, then offers to restart the app

### Library Tab
- Import character card PNGs (supports V2 `chara` and V3 `ccv3` tEXt chunks) and JSON card files
- **Drag-and-drop import** — drop PNG or JSON files anywhere on the tab to import them
- **Async import with progress** — imports run on a background thread (`import_worker.py`) with a cancellable progress dialog, so large batches don't freeze the UI; duplicates are skipped and reported, with a one-click "import anyway" follow-up pass
- Grid view with thumbnails and favorites indicators
- **Lazy thumbnail loading** — images load off the UI thread via a thread-pool worker with an LRU pixmap cache (`async_image.py`); the grid stays responsive for large libraries
- **Thumbnail zoom** (Ctrl+=/Ctrl+-) with View menu controls, clamped to 80–280px; reloads are debounced so rapid zoom only triggers one round of image reloads- **Multi-select** — Ctrl+click toggles individual cards, Shift+click selects a range; the detail pane switches to bulk actions (Bulk Delete / Favorite / Export PNG / Export JSON) when several cards are selected
- Search by name, description, tags, creator, and creator notes
- **Tag filter dialog** — a popup ("Select Tags…" in the filter bar) lists every known tag as a full-width, word-wrapped checkable list (with Select All / Select None), so long tag names are never clipped; the active filter shows selected tags as removable chips above the grid
- **Favorites only** checkbox to filter the grid to starred cards (toggle via F key)
- **Sort dropdown** (Name, Date Added, Token Count, Favorites, Rating, Random) with a synced View > Sort submenu
- **Star ratings** — click 0–5 stars in the detail pane; sort by Rating surfaces your best cards first (stored locally in the database, not embedded in the card file)
- **Collections** — group cards into named collections (a card can belong to many); filter via the collection dropdown ("All Cards" / "No Collection" / each collection), manage via the Manage... dialog, and assign from the detail pane's Collections button
- Toggle favorites with star indicator
- Double-click thumbnails for full-size image view with zoom/pan
- Export cards to PNG files
- **Open containing folder** button (selects the file in Explorer on Windows; opens the parent folder in Finder/Files on macOS/Linux)
- **Duplicate card** action (Ctrl+D) clones the selected card with a "(copy)" suffix
- Duplicate detection on import
- **Find Duplicates** scanner — groups cards by name+creator or by image hash; delete selected or keep-one-delete-rest (View > Find Duplicates)
- **Statistics** dashboard — library-wide stats with bar charts (View > Statistics)

### Edit Tab
- Full character card editor (name, description, personality, scenario, first message, etc.)
- **Unsaved-changes tracking** — dirty indicator with `_loading` guard prevents false positives during programmatic field population
- Multi-line alternate greetings editor (preserves embedded newlines)
- Tag management with add/remove
- **Tag autocomplete** — the tag input suggests existing tags from the library (contains-match, case-insensitive); the model refreshes whenever the library changes
- **Tag Manager** dialog — rename, merge, or delete tags across the entire library (each operation is a single atomic transaction)
- **Character Book editor** — dialog for editing lorebook entries (name, keys, content, insertion order, depth, case-sensitivity, etc.) plus an **Advanced** section with secondary trigger keys and a type-aware editor for unmodeled extension fields (uid, probability, sticky, ...)
- **Extensions editor** — dialog for editing the card's `extensions` dict with arbitrary JSON values (string/number/bool/object/array/null)
- Change card image (preserves embedded card data)
- **Favorite toggle** — a Favorite button next to Preview HTML mirrors the Library tab's star: it reads/writes the DB flag immediately (no save needed), keeps the in-memory card in sync so a later Save can't revert it, and refreshes the sidebar/grid thumbnails
- **My Notes** — a private per-card notes editor (stored in the local database only; never written to the card file)
- **Open containing folder** button
- Live token count with debounced updates (uses tiktoken)
- Save/Export/Revert functionality (Ctrl+S / Ctrl+R)
- Preserves favorite status, character book, extensions, and spec version on save

### Generate Tab
- Connect to any OpenAI-compatible API (LM Studio, Ollama, OpenRouter, OpenAI, Custom)
- **Multiple provider profiles** — each provider keeps its own base URL, API key, model, and sampling settings; switch the active provider in Settings and all generation/chat uses the selected profile
- Generate tags for existing characters
- **Generate Missing Tags** — suggest only new tags not already on the card (existing tags are preserved; never replaces them)
- Generate summaries (saved to creator notes)
- **Create New Character wizard** — step-by-step guided creation (name, appearance, personality, scenario, first message) from a concept
- **Fill Missing Fields** — generate any combination of missing fields (description, personality, scenario, first message, example messages, creator notes, system prompt, post-history instructions, tags, alternate greetings) with per-field target lengths
- **Batch generate** — missing tags and/or summaries for all cards via a dedicated dialog with progress
- **Create New Character dialog** — quick blank-card creation (name + optional image) as an alternative to the full step-by-step wizard
- **Streaming output** — generation results stream into the result pane token-by-token; a "Generating..." indicator shows while a request is in flight
- Fetch available models from the API endpoint
- **Cross-platform API key encryption** — keys are encrypted with Windows DPAPI on Windows and with Fernet (AES-CBC + HMAC via the `cryptography` package, key stored owner-only under `~/.st-explorer/secret.key`) on macOS/Linux; plain-text fallback (with a logged warning) only if encryption is unavailable. Keys saved by older versions keep decrypting
- Cooperative cancellation for all generation tasks

### Lorebooks Tab
- **Standalone lorebook library** — create, duplicate, rename, and delete world-info books stored as JSON under `~/.st-explorer/lorebooks/` (changes autosave)
- **Full entry editor** — per-entry name, trigger keys, content, position, insertion order, depth, enabled/case-sensitive/whole-word flags; add/edit/duplicate/remove/reorder via dialog or double-click; an Advanced section adds secondary keys and full extension-field editing; book-level extension fields are editable via the Extensions... button
- **Book settings** — name, description, scan depth, token budget, and recursive scanning, mirroring the character-book fields embedded in cards
- **AI generation** — *Generate Full Book* builds a complete set of entries from a concept (with optional card context from the shared sidebar so lore stays consistent — toggleable via the Use card context checkbox and persisted across sessions), *Generate More Entries* appends without duplicating existing topics, and *Generate Content* fills a selected entry's text from its keys/book/card context; results stream into a review pane before Apply
- **Import/export** — import both ST Explorer character-book JSON **and** SillyTavern native world-info JSON (auto-detected); export to either format
- **Test-tab injection** — pick active books in the Test tab's Lorebooks button; matching entries are scanned every turn and injected as `[World Info]` alongside the card's own book (per-book scan depth/budget/recursion), visible in the Context inspector

### Test Tab
- Per-character chat testing, with a card list on the left and a chat window on the right
- **Lorebook (world info) injection** — the card's character book is scanned on every turn: enabled entries whose keys match recent messages (honoring case-sensitivity and whole-word options) are appended to the system prompt under `[World Info]`, with recursive scanning support (entries triggering other entries) and the book's token budget enforced
- **Standalone lorebook injection** — books from the Lorebooks tab can be toggled active via the Lorebooks button; their matching entries join the same `[World Info]` block, each book honoring its own scan depth/budget, and the selection persists across sessions
- **Context inspector** — the Context button shows exactly what the next request will send, section by section (system prompt, world info, chat memory, each message) with per-section and total token counts against the configured context size
- **Streaming chat** with any character card using the configured API; multi-turn history is sent to the API and responses stream in as they arrive
- **Regenerate** the last assistant reply, and **Cancel** an in-flight response (the cancel button is disabled unless a request is running)
- Inline formatting rendered in distinct colors: `"dialogue"`, `*action*`, and `_emphasis_`
- **Per-message actions** — every bubble has Edit (reopen the message in a text dialog), Copy (to clipboard), and Delete (removes that message and everything after it); the last assistant bubble also gets a Regenerate button
- **File attachments** — attach text files (`.txt`, `.md`, `.json`, `.csv`, code, etc.) or images (`.png`, `.jpg`, `.gif`, `.webp`, `.bmp`) to a message via the Attach button. Text is inlined as a labelled block; images are downscaled, JPEG-encoded, and sent as multimodal `image_url` parts. Pending attachments appear as removable chips above the input
- **Inline URL images** — image URLs in assistant responses are fetched asynchronously and shown as clickable thumbnails (click to open in browser)
- **Chat memory** — a Memory dialog manages per-session memory entries (add/edit/delete), which are appended as a `[Chat memory]` bullet list to the system prompt (most recent 20 entries)
- **Auto-summarize** — toggle to automatically summarize each exchange into a memory entry after every assistant reply; "Summarize Now" summarizes the current conversation on demand
- **Auto-saved sessions** — each card's conversation is saved automatically (JSON files under `~/.st-explorer/sessions/`); a **Sessions** button opens a popup to load or delete saved sessions (with title, message count, and timestamps), and **New Session** starts fresh
- **Chat export** — export the current conversation as `.txt` or `.json` (JSON includes messages, memories, and metadata)
- `{{user}}` / `{{char}}` and custom macros are substituted automatically; double-click a card to open its full-size image

### Settings
- A single **Settings** dialog (menu bar **Settings > Settings...** or the gear button on the Generate/Test tabs) groups every option:
   - **API** — active provider selector plus per-provider base URL, API key, and model (with fetch-models)
   - **LLM** — temperature, top-p, top-k, min-p, context size, output length (max tokens), frequency/presence penalties, seed, automatic retries (0–5) with exponential backoff for connection errors, timeouts, rate limits (HTTP 429), and server errors
  - **Macros** — the `{{user}}` value plus custom `{{macro}}` overrides
  - **Prompts** — the tag/summary/character/fill/wizard/chat/memory-summary prompt templates
  - **Test** — chat display colors, timestamps, auto-scroll, max history, first-message greeting

### SillyTavern Integration
- **File-system sync** with a SillyTavern character library directory — works whether ST is running or not (ST doesn't lock or watch files; it picks up external changes via its mtime-keyed cache)
- **Two-way sync dialog** (SillyTavern > Sync Library, `Ctrl+Shift+L`) — grouped tree view showing every card pair classified as: Only in ST Explorer, Only in SillyTavern, In Sync, Changed in ST, Changed in Explorer, Conflict (both changed), or Unlinked Match
- **Two-way lorebook sync** (SillyTavern > Sync Lorebooks..., `Ctrl+Shift+W`) — compares the Lorebooks tab against ST's `worlds/` directory by filename, classifies each book as Only Explorer / Only ST / In Sync / Changed per side / Conflict using an ST-normalized content hash plus a baseline state file, and pushes/pulls through the native world-info converter; per-book Push/Pull plus bulk Pull All / Push All / Sync All with cancellable progress. The worlds directory is auto-derived from the characters path and overridable in Configure
- **Per-card actions** — Pull from ST, Push to ST, Link, Unlink directly from the sync dialog
- **Bulk "Sync All"** — executes a background sync plan (Pull new/changed-from-ST, Push new/changed-from-Explorer, Link matching pairs) with a cancellable progress dialog
- **Push/Pull selected** — push the currently selected card to ST or pull its ST version via the SillyTavern menu (background worker, never blocks the UI)
- **Push/Pull All** — one-click bulk push (all new/changed Explorer cards) or pull (all new/changed ST cards) from the SillyTavern menu, with a cancellable progress dialog. Bulk operations are non-destructive: they only copy/import and never delete files on either side; cards changed locally are skipped by Pull All so unpushed edits can't be overwritten
- **Refresh ST Status** — re-scan the ST directory and update the status-bar indicator on demand
- **Change detection** — content-hash baseline tracking identifies which side changed since the last sync; conflicts are flagged for manual resolution
- **Linking** — cards can be linked to specific ST character files (by avatar URL); linked cards always sync to/from the same file (case-insensitive on Windows)
- **Filename convention** — pushed cards use ST's sanitize-filename + collision suffix (`_1`, `_2`, …) naming so they drop in cleanly (Windows reserved device names like `CON`/`NUL` are handled)
- **Favorites sync** — favorites are stored inside the card PNG (`fav` / `data.extensions.fav`), so they travel automatically with push/pull — no special handling needed
- **Auto-detect** — on first launch, common SillyTavern install locations are scanned; if exactly one is found, it's auto-configured
- **Directory watcher** — a `QFileSystemWatcher` monitors the ST characters directory and shows a status-bar message when changes are detected
- **Status bar indicator** — permanent widget showing ST connection state and card count

## Installation

### Prerequisites
- Python 3.11+
- Windows 10+, macOS 12+, or a modern Linux desktop

On Linux, PyQt6 additionally needs the usual runtime libraries (`libxcb-*`, `libgl1`, fontconfig); on Debian/Ubuntu `sudo apt install python3-pyqt6` or the `libxcb-cursor0` package resolves most missing-plugin issues.

### Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

### Importing Cards
1. Go to the **Library** tab
2. Click **Import Card** (or drag-and-drop PNG files onto the tab)
3. Select one or more PNG character card files
4. Duplicate detection will warn you if a similar card already exists

### Editing Cards
1. Select a card in the Library or Edit tab
2. Edit any fields in the form
3. Click **Save** to write changes back to the PNG file
4. Use **Change Image** to replace the card's image while preserving metadata

### AI Features
1. Go to the **Generate** tab
2. Click **Settings** (or menu **Settings > Settings...**) and configure your endpoint
3. Choose a mode:
   - **Generate Tags**: Creates tags for the selected character
   - **Generate Missing Tags**: Suggests only new tags, preserving existing ones
   - **Generate Summary**: Creates a summary saved to creator notes
   - **Fill Missing Fields**: Generates the selected missing fields for the current card
   - **Create New Character**: Launches a step-by-step wizard to build a full character card
   - **Batch Generate**: Fills in missing tags/summaries for all cards

### Lorebooks
1. Go to the **Lorebooks** tab and click **New** (or **Import...** to load SillyTavern world-info JSON)
2. Add entries manually (**Add** / double-click) or generate them:
   - Type a concept, set the entry count, then click **Generate Full Book** or **Generate More Entries**
   - Select an entry and click **Generate Content** to fill its text from its keys
   - Select a character in the sidebar first to keep the generated lore consistent with it
3. Review the streamed output and click **Apply Result** — changes autosave
4. To use a book while chatting, go to the **Test** tab, click **Lorebooks**, and check the books to inject; matching entries are added as `[World Info]` on every turn

### Test Chat
1. Go to the **Test** tab
2. Select a character from the list on the left
3. Type a message and press Enter — responses stream back token-by-token
4. Use **Regenerate** to redo the last reply or **Cancel** to interrupt an in-flight response
5. Use **Attach** to add text/image files, and the **Memory** button to manage persistent memory (or toggle **Auto-Summarize**)
6. Sessions auto-save per character; use **Sessions** to load or delete past conversations, and **Export** to save as `.txt`/`.json`

### SillyTavern Sync
1. Go to **SillyTavern > Configure** and set the path to your SillyTavern `characters/` directory (e.g. `C:\SillyTavern\data\default-user\characters`), or use Auto-Detect
2. Go to **SillyTavern > Sync Library** (or press `Ctrl+Shift+L`)
3. The dialog scans both libraries and shows each card pair with a status badge:
   - **Only in ST Explorer** — push to ST
   - **Only in SillyTavern** — pull into ST Explorer
   - **In Sync** — no action needed (or Link if matched but not yet linked)
   - **Changed in ST** — pull to update your ST Explorer copy
   - **Changed in ST Explorer** — push to update ST
   - **Conflict** — both sides changed; choose Push or Pull manually
   - **Unlinked Match** — same name+creator but not linked; Link to connect them
4. Use per-card **Pull / Push / Link / Unlink** buttons, or **Sync All** for bulk sync
5. For a single card, use **SillyTavern > Push/Pull Selected** from the menu while a card is selected on the Edit / Generate / Test tabs

### Lorebook Sync
1. Configure SillyTavern — the World Info directory is derived automatically (e.g. `characters` -> `worlds`) and can be overridden
2. Go to **SillyTavern > Sync Lorebooks...** (`Ctrl+Shift+W`)
3. Each book pair is classified: Only Explorer / Only ST (push/pull to import), In Sync, Changed on one side (safe direction), or Conflict (both changed — resolve per-book)
4. Use per-book **Push / Pull** buttons or bulk **Pull All / Push All / Sync All**; bulk operations are non-destructive and never touch conflicted books
5. Books are matched by filename (ST's own identity for world info) and compared via an ST-normalized content hash, so Explorer-schema and native ST JSON of the same book always compare equal

### Keyboard Shortcuts

All shortcuts are defined in a central registry (`src/ui/shortcuts.py`) and appear in the menu bar.

| Shortcut | Action |
|----------|--------|
| Ctrl+I | Import Cards |
| Ctrl+E | Export as PNG |
| Ctrl+Q | Quit |
| Ctrl+S | Save (Edit tab) |
| Ctrl+R | Revert (Edit tab) |
| Ctrl+D | Duplicate Card |
| Del | Delete Card |
| Ctrl+F | Find (focus search) |
| F | Toggle Favorites Only |
| F5 | Refresh |
| Ctrl+= | Zoom In |
| Ctrl+- | Zoom Out |
| Ctrl+Shift+L | Sync Library with SillyTavern |
| Ctrl+Shift+W | Sync Lorebooks (world info) with SillyTavern |
| Ctrl+, | Settings |
| File > Export as JSON | Export selected card as JSON |
| File > Backup Now | Create a manual database backup |
| File > Backup Library... | Zip the entire library (DB + cards + sessions + thumbnails) |
| File > Restore Library... | Restore a library backup zip (prompts restart) |
| View > Font Size... | Set the application font size |
| View > Find Duplicates | Duplicate scanner dialog |
| View > Statistics | Library statistics dashboard |
| SillyTavern > Configure | Configure SillyTavern directory |
| SillyTavern > Push Selected | Push the selected card to ST |
| SillyTavern > Pull Selected | Pull the selected card's ST version |
| SillyTavern > Push All to SillyTavern | Bulk-push all new/changed cards to ST |
| SillyTavern > Pull All from SillyTavern | Bulk-pull all new/changed cards from ST |
| SillyTavern > Refresh ST Status | Re-scan ST directory |
| Help > About | About ST Explorer |
| Help > View Log | View Application Log |

## Character Card Format

ST Explorer supports the SillyTavern character card specification:
- **V2** (`chara` keyword): Base64-encoded JSON in a PNG tEXt chunk
- **V3** (`ccv3` keyword): Same encoding, updated spec

When saving, both V2 and V3 chunks are written for maximum compatibility.

## Data Storage

- Library database: `~/.st-explorer/library.db` (SQLite)
- Card files: `~/.st-explorer/library/`
- Thumbnails: `~/.st-explorer/thumbnails/`
- AI-generated cards: `~/.st-explorer/generated/`
- Standalone lorebooks: `~/.st-explorer/lorebooks/*.json`
- Chat sessions: `~/.st-explorer/sessions/` (one JSON file per card per session)
- Application log: `~/.st-explorer/app.log`
- Lorebook sync baseline: `~/.st-explorer/lorebook_sync_state.json`
- API-key encryption key (macOS/Linux only): `~/.st-explorer/secret.key` (created on first save, owner-only permissions)

Settings are stored by QSettings' native backend per platform:

| Platform | Location |
|----------|----------|
| Windows  | Registry — `HKCU\Software\STExplorer` |
| macOS    | `~/Library/Preferences/com.stexplorer.stexplorer.plist` |
| Linux    | `~/.config/STExplorer/STExplorer.ini` |

This includes `st/characters_path` for the SillyTavern directory and `st/worlds_path` for the world-info (lorebooks) directory. Encrypted API keys live alongside them (`dpapi1:`-prefixed blobs on Windows, `fernet1:`-prefixed tokens elsewhere).

## Platform Support

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| Settings backend | Registry | plist | INI file |
| API-key encryption | DPAPI (`crypt32`) | Fernet + `secret.key` | Fernet + `secret.key` |
| Single-instance lock | `msvcrt` file lock | `flock` | `flock` |
| Open containing folder | Explorer (selects file) | Finder (opens folder) | Files/default manager (opens folder) |
| Missing-dependency notice | Native message box | Qt dialog / terminal | Qt dialog / terminal |
| SillyTavern auto-detect | Drive roots, home & Documents | `/Applications`, `~/Applications`, home & Documents | `/opt`, `/srv`, `~/.local/share`, home & Documents |
| Atomic writes | Retry transient locks | Direct replace | Direct replace |

Keyboard shortcuts use the platform modifier automatically (Qt maps Ctrl to Cmd on macOS).

## Tech Stack

- **PyQt6** - GUI framework
- **Pillow** - PNG image handling
- **tiktoken** - Token counting (with byte-length fallback)
- **requests** - API client
- **cryptography** - Cross-platform API-key encryption (Fernet on macOS/Linux)
- **SQLite** - Library database
