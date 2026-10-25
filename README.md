# ST Explorer

A desktop application for browsing, editing, and managing SillyTavern character cards. It provides a visual library for character card PNG files with embedded JSON metadata (V2/V3 spec) and Lorebooks.

![Image: images/Library.jpg](./images/Library.jpg)

## Features

### Application
- **Data encryption (opt-in)** — a password-protected vault (Settings > Encryption) encrypts the database, cards, thumbnails, chat sessions, lorebooks, gallery images, and backups with AES-256-GCM; the app asks for the password at startup and seals the database again on exit. An optional recovery key can unlock the library if the password is forgotten. Exports and SillyTavern sync copies always stay plain text
- **Duplicate scanner** — finds cards sharing the same name + creator (case-insensitive), or near-identical images via perceptual hashing; tree view with thumbnails, delete selected, or keep one and delete the rest of a group (View > Find Duplicates)
- **Statistics dashboard** — aggregate metrics rendered as colored bars: total/avg/min/max tokens, top-10 tags, spec version distribution, creator leaderboard, and cards-added-per-week sparkline (View > Statistics)
- **Full-library backup & restore** — File > Backup Library zips the database, all card files, chat sessions, thumbnails, and gallery images into a single archive with a manifest; File > Restore Library validates and swaps a backup back in atomically, then offers to restart the app. A restore is **non-destructive**: it stages and validates everything first, honours cancellation before touching live data, moves each live directory aside instead of deleting it, and rolls back on failure. Directories the archive doesn't contain are left alone rather than wiped
- **Library integrity check ("Doctor")** — File > Check Library audits the whole library and reports each problem with its suggested repair: cards whose file is missing, files that hold no character data any more, malformed tag values, stray files in the library folder, chat sessions belonging to deleted cards, gallery entries whose image is gone or whose folder belongs to a deleted card, SillyTavern links whose file has vanished, stale deletion records, API keys that can no longer be decrypted, and SQLite's own integrity check. **Preview Fixes** is a dry run: it lists exactly which rows would be dropped and which folders would be deleted, and you can untick the destructive ones before applying. No automatic fix ever deletes a card file, and a repair that removes a card row whose file has vanished keeps that card's chat history and gallery images. The check runs in a background thread, so the window stays responsive
- **Command palette** — `Ctrl+K` (or `Ctrl+P`) opens a searchable list of every enabled menu action; typing filters by fuzzy subsequence and the best match is ranked first (an exact prefix beats a match at a word boundary, which beats a match anywhere), the arrow keys move the selection and `Enter` runs the action. The palette follows the active theme
- **Card linter** — View > Check Card reports the quality problems of the selected card: empty required fields, a missing first message, an over-long description, a token budget overrun, an unbalanced example dialogue, character-book entries without content or keys, placeholder text, and text that looks like a prompt-injection attempt
- **App settings** — the Settings > App tab holds the spell-check toggle, the application-log switch (toggling it live adds/removes the `app.log` file handler), and the UI font size (the same setting as View > Font Size...)
- **Live token count** with debounced updates

### Library Tab
- Import character card PNGs (supports V2 `chara` and V3 `ccv3` tEXt chunks) and JSON card files
- **Drag-and-drop import** — drop PNG or JSON files anywhere on the tab to import them
- **Search** by name, description, tags, creator, and creator notes - synced with the shared sidebar's search box. Two extra modes widen it: **Full text** also searches the whole card (the description, the other prompt fields, the creator notes and the character book), and **Fuzzy** also matches cards whose name, creator or one of their tags contains every word of the query, in any order
- **Tag filter dialog** — a popup ("Select Tags…" in the filter bar) lists every known tag as a full-width, word-wrapped checkable list (with Select All / Select None), so long tag names are never clipped; the active filter shows selected tags as removable chips above the grid
- **Favorites only** checkbox to filter the grid to starred cards (toggle via F key)
- **Star ratings** — click 0–5 stars in the detail pane; sort by Rating surfaces your best cards first (stored locally in the database, not embedded in the card file)
- **Collections** — group cards into named collections (a card can belong to many); filter via the collection dropdown ("All Cards" / "No Collection" / each collection), manage via the Manage... dialog, and assign from the detail pane's Collections button
- Toggle favorites with star indicator
- Double-click thumbnails for full-size image view with zoom/pan
- Export cards to PNG or JSON files (File > Export as PNG / Export as JSON)
- **Find Duplicates** scanner — groups cards by name+creator or by image hash; delete selected or keep-one-delete-rest (View > Find Duplicates)

### Edit Tab
- **Full character card editor** (name, description, personality, scenario, first message, etc.)
- **Tag Manager** dialog — rename, merge, or delete tags across the entire library (each operation is a single atomic transaction)
- **Tag autocomplete** — the tag input suggests existing tags from the library (contains-match, case-insensitive); the model refreshes whenever the library changes
- **Character Book editor** — dialog for editing lorebook entries (name, keys, content, insertion order, depth, case-sensitivity, etc.) plus an **Advanced** section with secondary trigger keys and a type-aware editor for unmodeled extension fields (uid, probability, sticky, ...)
- **Extensions editor** — dialog for editing the card's `extensions` dict with arbitrary JSON values (string/number/bool/object/array/null)
- **Change card image** (preserves embedded card data)
- **Rename file on name change** — saving with a changed character name renames the card file (and thumbnail) in the library and reloads the editor from the renamed file; the card's database id and SillyTavern link stay the same, so chat sessions and syncing are unaffected

### Gallery Tab
- **Per-character gallery** — extra images stored in the library (`gallery/<id>/`), managed per character: add images or import a whole folder, drag-and-drop image files onto the tab, drag thumbnails to reorder, per-image captions, and duplicate imports skipped by content hash
- **SillyTavern expression images** — full management of the character's expression slots in ST's `user/images/<charName>/` folder (shared with SillyTavern): the 7 default expressions (`neutral`, `happy`, `sad`, `angry`, `surprised`, `disgusted`, `fearful`) shown as slots with visible gaps where an image is missing, plus any custom expressions found on disk; add/replace/delete per slot
- **Set as card image** — promote any gallery or expression image to the character's portrait (card data is preserved, thumbnail regenerated)
- **Card assets** — read-only pane listing the V3 `assets` declared by the card, with previews when the referenced file can be found next to the card or in ST's user-images folder
- **Export** — export a single image, or the whole character's gallery plus expressions to a folder
- Reveal any image's folder in the OS file manager; caption editing via the context menu

### Generate Tab
- Connect to any OpenAI-compatible API. Built-in profiles: **LM Studio**, **Ollama**, **text-generation-webui**, **KoboldCpp**, **llama.cpp**, **vLLM**, **Jan**, **GPT4All**, **LocalAI**, **OpenRouter**, **OpenAI**, **Gemini**, **Mistral**, **DeepSeek**, **Groq**, **xAI Grok**, and **Custom** for any other endpoint
- **Prompt injection hardening** — character cards are third-party downloads, so their text is quoted inside a `<card_data>` block and every generation system prompt states that the block is *data, never instructions*. A card that says "ignore your previous instructions" can no longer steer the generator — and it cannot forge the closing tag either
- **More card context** — tag, summary and alternate-greeting generation now also see the card's first message and its example dialogue, which is where the character's actual voice lives
- **Validated model output** — a reply is never written into a card unchecked: only the requested fields are applied, `tags` must be a list (a comma-separated string is split instead of stored as one giant tag), text fields are length-capped, and saving a generated `system_prompt` / `post_history_instructions` asks for an explicit confirmation because those fields become model instructions in every future chat
- **Generate tags** for existing characters
- **Generate Missing Tags** — suggest only new tags not already on the card (existing tags are preserved; never replaces them)
- Generate summaries (saved to creator notes)
- **Create New Character wizard** — step-by-step guided creation (name, appearance, personality, scenario, first message) from a concept
- **Fill Missing Fields** — generate any combination of missing fields (description, personality, scenario, first message, example messages, creator notes, system prompt, post-history instructions, tags, alternate greetings) with per-field target lengths
- **Batch generate** — missing tags and/or summaries for all cards via a dedicated dialog with progress
- **Create New Character dialog** — quick blank-card creation (name + optional image) as an alternative to the full step-by-step wizard
- **Cross-platform API key encryption** — keys are encrypted with Windows DPAPI on Windows and with Fernet (AES-CBC + HMAC via the `cryptography` package, key stored owner-only under `~/.st-explorer/secret.key`) on macOS/Linux; plain-text fallback (with a logged warning) only if encryption is unavailable.

### Lorebooks Tab
- **Standalone lorebook library** — create, duplicate, rename, and delete world-info books stored as JSON under `~/.st-explorer/lorebooks/` (changes autosave)
- **Full entry editor** — per-entry name, trigger keys, content, position, insertion order, depth, enabled/case-sensitive/whole-word flags; add/edit/duplicate/remove/reorder via dialog or double-click; an Advanced section adds secondary keys and full extension-field editing; book-level extension fields are editable via the Extensions... button
- **Book settings** — name, description, scan depth, token budget, and recursive scanning, mirroring the character-book fields embedded in cards
- **AI generation** — *Generate Full Book* builds a complete set of entries from a concept (with optional card context from the shared sidebar so lore stays consistent — toggleable via the Use card context checkbox and persisted across sessions), *Generate More Entries* appends without duplicating existing topics, and *Generate Content* fills a selected entry's text from its keys/book/card context; results stream into a review pane before Apply
- **Import/export** — import both ST Explorer character-book JSON **and** SillyTavern native world-info JSON (auto-detected); export to either format

### Test Tab
- **Per-character chat testing**
- **Lorebook injection** — books from the Lorebooks tab can be toggled active via the Lorebooks button; their matching entries join the same `[World Info]` block, each book honoring its own scan depth/budget, and the selection persists across sessions
- **Context inspector** — the Context button shows exactly what the next request will send: the **Prompt** tab lists every block of the request (system prompt, world info, persona, attributes, memory, example dialogue, each message and each depth injection) with the token cost of each one, a context-pressure meter that turns amber above 70 % and red above 100 %, and a "Payload" row with the exact messages that would be posted. The preview follows the send path exactly — the same system assembly, the same trailing blocks (example dialogue, post-history instructions, jailbreak) and the same trim, so it cannot drift from what the provider receives. The other two tabs edit the chat's Author's Note and Jailbreak, and the prompt preview updates live while you type in them
- **Persona pairings** — the Pairings button saves the current character together with the persona you are chatting with, so you can jump back into the right voice later; the manager can export and import the list
- **Chat export** — export any saved chat from the Chats window as `.txt`, `.json` (messages, memories, and metadata), `.md` (Markdown), `.html` (a standalone page), or a SillyTavern `.jsonl` chat
- **Inline formatting** rendered in distinct colors: `"dialogue"`, `*action*`, and `_emphasis_`
- **Alternate responses** — ▶ steps through a reply's stored alternatives, ▶ (New) generates another one, and Regenerate replaces them all; Delete on a reply with alternatives removes only the currently displayed response (the others and later messages are kept)
- **File attachments** — attach text files (`.txt`, `.md`, `.json`, `.csv`, code, etc.) or images (`.png`, `.jpg`, `.gif`, `.webp`, `.bmp`) to a message via the Attach button.
- **Inline URL images** — image URLs in assistant responses are fetched asynchronously and shown as clickable thumbnails (click to open in browser)
- **Chat memory** — a Memory dialog manages per-session memory entries (add/edit/delete), which are appended as a `[Chat memory]` bullet list to the system prompt (most recent 20 entries)
- **Persistent attributes** — the Memory dialog also tracks watched story state that stays current through long roleplays (defaults: Setting, `{{char}}` mood, `{{char}}` clothing, Time & weather, `{{char}}` physical state, `{{user}}`-`{{char}}` relationship, Current activity, Who is present). "Auto-update each exchange" (on by default) asks the model after every reply which attributes changed and rewrites only those as short facts; "Update Now" runs the same pass over the whole conversation on demand. Rows are fully customizable — rename, add, remove, toggle per-row on/off, edit values by hand, and tune the "What To Watch For" hints. Current values are injected as a `[Persistent attributes]` block between the user persona and chat memory (SillyTavern prompt order; `{{char}}`/`{{user}}` macros in names are resolved; empty values are omitted), and everything is stored per chat session
- **Auto-memory** — the Auto-Memory toggle automatically summarizes each exchange into a memory entry after every assistant reply; "Summarize Now" summarizes the current conversation on demand
- **Auto-saved chats** — each card's conversation is saved automatically (JSON files under `~/.st-explorer/sessions/`); a **Chats** button opens a popup to load, export, import, or delete saved chats (with title, message count, and timestamps), and **New Chat** starts fresh
- **Macros** — `{{time}}`, `{{date}}`, `{{datetime}}`, `{{random:a|b|c}}` / `{{pick: a, b}}`, and dice rolls `{{roll: 2d6+3}}` on top of `{{user}}` / `{{char}}` and custom macros
- **Persona manager** — named `{{user}}` personas (name + description) with a per-chat selection, injected as the `[User persona]` block

### SillyTavern Integration
- **File-system sync** with a SillyTavern character library directory — works whether ST is running or not (ST doesn't lock or watch files; it picks up external changes via its mtime-keyed cache)
- **Two-way sync dialog** (SillyTavern > Sync Library, `Ctrl+Shift+L`) — grouped tree view showing every card pair classified as: Only in ST Explorer, Only in SillyTavern, In Sync, Changed in ST, Changed in Explorer, Conflict (both changed), or Unlinked Match
- **Two-way lorebook sync** (SillyTavern > Sync Lorebooks..., `Ctrl+Shift+W`) — compares the Lorebooks tab against ST's `worlds/` directory by filename, classifies each book as Only Explorer / Only ST / In Sync / Changed per side / Conflict using an ST-normalized content hash plus a baseline state file, and pushes/pulls through the native world-info converter; per-book Push/Pull plus bulk Pull All / Push All / Sync All with cancellable progress. The worlds directory is auto-derived from the characters path and overridable in Configure
- **Change detection** — content-hash baseline tracking identifies which side changed since the last sync; conflicts are flagged for manual resolution
- **Linking** — cards can be linked to specific ST character files (by avatar URL); linked cards always sync to/from the same file (case-insensitive on Windows)
- **Favorites sync** — favorites are stored inside the card PNG (`fav` / `data.extensions.fav`), so they travel automatically with push/pull — no special handling needed
- **Auto-detect** — on first launch, common SillyTavern install locations are scanned; if exactly one is found, it's auto-configured
- **Expression images** — the Gallery tab reads and writes each character's expression images directly in ST's `user/images/<charName>/` folder (plain files, so ST sees every change immediately); the folder is auto-derived from the characters path and overridable in Configure
- **Directory watcher** — a `QFileSystemWatcher` monitors the ST characters directory and shows a status-bar message when changes are detected

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
3. Pick a system prompt from the **System Prompt** dropdown (Natural Chat, Simple Roleplay, ...) or keep Custom; create your own in **Settings > Prompts > Manage System Prompts...**. Optionally pick a **Context** template (framing/names/example formatting) or manage them in **Settings > Prompts > Manage Context Templates...**
4. Type a message and press Enter — responses stream back token-by-token
5. Use **Regenerate** to redo the last reply or **Cancel** to interrupt an in-flight response
6. Use **Attach** to add text/image files, and the **Memory** button to manage persistent memory (or toggle **Auto-Memory**)
7. Chats auto-save per character; use **Chats** to load, export, import, or delete past conversations
8. Click **Prompt** to see the exact prompt the next request will send - every block with its token cost, the context-pressure meter and the payload that would be posted

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

### Data Encryption
1. Go to **Settings > Encryption** and click **Enable Encryption...**
2. Choose a password (and optionally keep the generated recovery key somewhere safe — it is shown only once)
3. The library is encrypted in place with a progress dialog; the database is sealed automatically when the app closes
4. The next launch asks for the password before opening the library; wrong passwords never touch any file
5. **Change Password...** is instant (only the key is re-wrapped), and **Disable Encryption...** decrypts everything back to plain text after confirming the password

### Keyboard Shortcuts

All shortcuts are defined in a central registry (`src/ui/shortcuts.py`) and appear in the menu bar.

| Shortcut | Action |
|----------|--------|
| Ctrl+I | Import Cards |
| Ctrl+E | Export as PNG |
| Ctrl+Q | Quit |
| Ctrl+S | Save (Edit tab) |
| Ctrl+R | Revert (Edit tab) |
| Ctrl+Z | Undo (Edit tab) |
| Ctrl+Shift+Z | Redo (Edit tab) |
| Ctrl+D | Duplicate Card |
| Del | Delete Card |
| Ctrl+F | Find (focus search) |
| F | Toggle Favorites Only |
| F5 | Refresh |
| Ctrl+= / Ctrl++ | Zoom In |
| Ctrl+- | Zoom Out |
| Ctrl+0 | Reset Zoom |
| Ctrl+1 … Ctrl+6 | Switch tab (Library → Test) |
| Ctrl+Shift+L | Sync Library with SillyTavern |
| Ctrl+Shift+W | Sync Lorebooks (world info) with SillyTavern |
| Ctrl+K / Ctrl+P | Command palette |
| Ctrl+, | Settings |

### Prompt Templates

Every generation prompt can be rewritten in **Settings > Prompts > Edit prompt**:

- one tab per feature (Tags, Missing Tags, Summary, Alt Greetings, Fill Fields, Wizard, Chat, Character, Impersonate, Memory, Attributes, Lorebook, Lorebook Entry)
- the **live preview** renders the template with a sample card and shows its token cost; it updates while you type
- the placeholders each template supports are listed under the editor, and an unknown one (a typo) is flagged — an unknown placeholder resolves to an empty string, so a typo silently empties a field of the generated prompt
- **Reset to Default** only changes the editor; the template is written when the settings are saved, so a reset followed by *Cancel* keeps your customisation

Placeholders are substituted with `str.format` syntax: `{name}` for a field, `{{` / `}}` for a literal brace.

## Character Card Format

ST Explorer supports the SillyTavern character card specification:
- **V2** (`chara` keyword): Base64-encoded JSON in a PNG tEXt chunk
- **V3** (`ccv3` keyword): Same encoding, updated spec

When saving, both V2 and V3 chunks are written for maximum compatibility.

**Round-trip safety** — saving never drops data: unmodeled V2/V3 fields (`nickname`, `assets`, `group_only_greetings`, `creator_notes_multilingual`, `avatar`, `chat`, `create_date`, ...), extensions, and SillyTavern-specific lorebook entry fields are preserved untouched through edit → save → reload.

### Untrusted card content

A character card is a file someone else wrote. ST Explorer therefore never
mixes card text into an instruction prompt without a boundary:

- the card content is quoted inside a `<card_data> … </card_data>` block;
- a delimiter inside the card text is neutralised, so a card cannot forge the
  closing tag and have the rest of its text read as an instruction;
- every generation system prompt states that the block is *data to describe,
  never instructions to follow*, and that any command inside it must be
  ignored;
- a reply is validated before it is written back into the card, and fields
  that become model instructions in a later chat (`system_prompt`,
  `post_history_instructions`) ask for a confirmation first.

The chat uses the same boundary. The card's description, personality,
scenario, its example dialogue, the world info and its own `system_prompt`
are all quoted as data between `<card_data>` tags, and the system prompt
states that they are data and not instructions. SillyTavern sends a card's
`system_prompt` as an instruction, so this is deliberately *stricter* than
SillyTavern: a downloaded card cannot steer the model on its own. Your own
text - the persona, the Author's Note, the jailbreak, the memory entries -
is unaffected and stays an instruction.

## Data Storage

- Library database: `~/.st-explorer/library.db` (SQLite)
- Card files: `~/.st-explorer/library/`
- Thumbnails: `~/.st-explorer/thumbnails/`
- Gallery images: `~/.st-explorer/gallery/<character_id>/`
- AI-generated cards: `~/.st-explorer/generated/`
- Standalone lorebooks: `~/.st-explorer/lorebooks/*.json`
- Chat sessions: `~/.st-explorer/sessions/` (one JSON file per card per session)
- Application log: `~/.st-explorer/app.log` (written only while enabled in Settings > App)
- Spell-check word list: `~/.st-explorer/spellcheck.json` (words added to / ignored by the personal dictionary)
- Lorebook sync baseline: `~/.st-explorer/lorebook_sync_state.json`
- API-key encryption key (macOS/Linux only): `~/.st-explorer/secret.key` (created on first save, owner-only permissions)
- Data-encryption envelope: `~/.st-explorer/vault.json` (when encryption is enabled)

### Data encryption at rest

When **Settings > Encryption > Enable** is used, everything under `~/.st-explorer` (database, cards, thumbnails, sessions, lorebooks, gallery images, generated cards, sync baseline, and the local `.bak` backups) is encrypted with AES-256-GCM under a random master key. The master key is wrapped by a key derived from your password (Scrypt) and stored in `vault.json`, so changing the password is instant and never re-encrypts files. An optional recovery key provides a second way to unwrap the master key.

- At startup the app asks for the password (or recovery key) before opening the database; the database file is decrypted in place for the session and re-sealed when the app exits.
- Files are encrypted on disk at all times and decrypted in memory on use; filenames remain visible in the session/library folders.
- Encrypted files start with a `STEV` magic prefix, so unencrypted leftovers from before enabling (or from a crash) keep working and are picked up by the next seal pass.
- **Exports** (PNG/JSON/chat) and **SillyTavern sync** copies are always plain text, since other tools must be able to read them.
- **Library backups** (File > Backup Library) store the data exactly as encrypted on disk, with the database sealed as it is added; restoring an encrypted backup requires the password. A plain-text backup restored into an encrypted library is re-sealed automatically.
- A forgotten password (and lost recovery key) means the data cannot be recovered; there is no back door. The app log is deliberately left in plain text for troubleshooting.

Settings are stored by QSettings' native backend per platform:

| Platform | Location |
|----------|----------|
| Windows  | Registry — `HKCU\Software\STExplorer` |
| macOS    | `~/Library/Preferences/com.stexplorer.stexplorer.plist` |
| Linux    | `~/.config/STExplorer/STExplorer.ini` |

This includes `st/characters_path` for the SillyTavern directory, `st/worlds_path` for the world-info (lorebooks) directory, and `st/user_images_path` for the expression-images directory. Encrypted API keys live alongside them (`dpapi1:`-prefixed blobs on Windows, `fernet1:`-prefixed tokens elsewhere).

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
