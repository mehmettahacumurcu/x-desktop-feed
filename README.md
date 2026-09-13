# X Desktop Feed

X Desktop Feed is a local PySide6 desktop application for saving public X posts, collecting
recent original posts from selected public profiles, and capturing bounded snapshots of the
signed-in user's X For You and Following timelines. Saved data, session history, topic models,
and downloaded photos stay on this computer in the platform application-data directory.

This is the source distribution of version 0.1.0. It requires a repository checkout and an
editable install; a standalone Windows executable or self-contained wheel is not supplied.
Collection and online embeds contact X, and photo downloads contact its media CDN. Storage and
topic analysis are local; this is not a network-isolated application.

## Setup and launch

Python 3.12 or newer is required. From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m xfeed
```

After that one-time setup, double-click **Start X Desktop Feed.cmd** in the repository root. It
starts the app through this repository's `.venv` without leaving a console window open. If the
environment is missing, the launcher explains what to create and waits so the error stays visible.

## Language

The interface is available in **English** and **Türkçe**. Pick a language from the dropdown in
the sidebar; the choice is remembered for the next launch. Feed pages render in the selected
language immediately.

## Feed tabs and session history

The app opens on **My Feed**, with separate **For You** and **Following** tabs. Each tab has its
own post amount, automatic interval, status, current-session reader, and older-history controls.
Both readers are intentionally empty at the start of a new desktop session. Choose `10`, `20`,
`30`, or `50` posts on the active tab and select **Collect now**. The extension first selects and
verifies the requested core X home tab, then extracts only that feed.

Captures made while the app remains open appear under **This session** in the matching feed tab.
Enable **Show older posts** in that tab to append its stored captures beneath an **Older app
sessions** divider, then use **Jump to older sessions** to move directly to it. For You history
never appears in the Following reader, and Following history never appears in the For You reader.

The **Automatic** menu offers **Never**, 5, 10, 30, and 60 minutes. The remembered setting runs
only while the desktop app is open and always waits one full interval before the first automatic
capture. If another collection is active when a timer becomes due, that automatic capture is
queued and serialized by the shared coordinator; the next full interval begins normally.

Use **Save URL** to open the separate **Manual Saves** drawer. Paste a supported `x.com` or
`twitter.com` post URL and select **Save**. Manual saves remain independently viewable in this
drawer and never mix into the current For You session. Saving an existing canonical URL does not
create another post row, but it does retain manual-save provenance and focuses the existing card.

On **Sources**, connect the desktop app to the bundled Opera GX extension:

1. Open `opera://extensions` in Opera GX.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select the absolute extension path shown by the desktop
   **Connect Opera GX** dialog.
   **After updating X Desktop Feed, reload this unpacked extension once.** The desktop/extension
   protocol is version 4, so the previously loaded extension cannot collect either home feed
   until Opera has loaded the updated files.
4. Copy the pairing code into the extension popup and select **Pair**.
5. Confirm the desktop app shows **Opera connected / X signed in**. If X is signed out, use
   **Open X in Opera**, sign in directly in Opera GX, and then choose **Check connection**.
6. Return to **My Feed** and select **Collect now**, or open **Sources** and collect a profile.
   A collection run briefly focuses the dedicated tab so X renders the timeline normally; leave
   that tab alone until the run completes.
7. Use **Disconnect Opera** to revoke the desktop pairing without signing out of X.

On **Sources**, enter `@openai` or another handle and select **Add profile**. Every profile has its
own 5, 10, 20, or 30 post amount and **Collect** action. The toolbar has a separate global amount
for **Collect all**; it processes enabled profiles sequentially in their displayed order. A
disabled profile stays visible and removable but is skipped by Collect all.

The checkbox on each profile controls viewing only. Select one or several profiles to combine
their locally saved posts in the right-hand reader, newest first and without duplicates. It does
not enable or disable collection. Drag the divider between profile cards and the reader to resize
the two areas; the app remembers that layout. Progress appears on the page that initiated the
work, and **Cancel** clears pending Collect all profiles while stopping the active run.

## Saved posts and export

The **Saved** tab in My Feed shows manually saved posts separately from the two captured home
feeds. The **Export** page lists nonempty application sessions and exports selected sessions to
CSV or JSON with `url`, `author`, `text`, and `date` fields. Posts are deduplicated across the
selected sessions. Manual saves are associated with sessions by timestamp overlap; collection
posts use their recorded session ID. This is a dataset export, not a full database/media backup.

Export retains original post text. CSV text can be interpreted as a formula by spreadsheet
software; use JSON for untrusted content or import CSV columns explicitly as text. The export
writer is not atomic and does not guarantee preservation of an existing file after a write
failure. Ensure the selected dialog format matches the filename extension. Dataset import and
trained unwanted-content classification are not
implemented as complete user workflows in this release.

## Local photo storage

X Desktop Feed automatically saves every static photo attached to a post that newly enters the
local library through **My Feed**, **Sources**, or **Manual Saves**. It keeps X display order and
stores display-sized copies: every photo is retained, with its longest edge capped at 1600 pixels.
Photos are stored in the application's local data directory, under
`media/<post-id>/photo-<position>.<ext>`.

Readers prefer the local gallery as soon as at least one saved photo is available; the duplicate
online embed media is then omitted. Until that happens, the normal online embed remains the
fallback. **Saving photos…** means the post is saved and its local photo work is still pending.
`N photo(s) couldn't be saved.` means those local copies failed, but the saved post remains
available and the online fallback is still used when no local photo is available.

If the app restarts, it resumes unfinished resolution and download jobs created for newly saved
posts by this feature. It does not scan existing posts or backfill their media. Version one does
not download videos or animated GIFs, provide cleanup controls or a Settings toggle, or retain
full-resolution originals.

## Insights

Open **Insights** to audit what has been saved locally. The **Dataset** filter selects the current
session, one past nonempty feed session, all feed sessions, or all saved posts. **All saved posts**
also includes Sources and Manual Saves. The **Feed** filter selects Combined, For You, or
Following; a feed-specific selection excludes posts seen only in the other feed, Sources, or
Manual Saves.

The summary uses these meanings:

- **Unique posts** deduplicates the selected dataset by saved post.
- **Appearances** counts captured feed observations, so one post can appear more than once.
- **Unique authors** counts distinct saved author handles.
- **With media** is the percentage of unique posts with known or locally available media.
- **New posts** and **Already saved** appear for one session and reconcile its collection runs.

The remaining sections show each post's primary local topic, latest observed post kind, top
authors, Combined feed balance, and multi-session trends. Topic analysis runs entirely on this
computer using the text of all saved posts. At least 10 posts with usable text are required; below
that minimum, Insights reports **Not enough text for reliable topics**. **Reanalyze** forces a new
local fit even when the saved corpus has not changed.

## Opera connection troubleshooting

- **Desktop app closed:** launch it before pairing or collecting; the local bridge is available
  only while the app is running.
- **Extension disabled:** re-enable X Desktop Feed on `opera://extensions`, then check the
  connection in the desktop app.
- **X signed out:** choose **Open X in Opera**, sign in manually, and choose **Check connection**.
- **Port 47831 occupied:** close the other process using local port 47831, then restart the
  desktop app. The bridge intentionally listens only on this fixed loopback port.
- **Challenge or rate limit:** resolve any challenge manually in Opera GX or wait for the rate
  limit to clear. The collector will not bypass either condition.
- **Extension reloaded:** the existing pairing remains valid after a normal extension reload.
  Generate a new code and pair again only if the extension's local storage was cleared.
- **App shows disconnected while the extension shows connected:** reload the extension once on
  `opera://extensions`. The desktop app keeps a sixty-second connection lease that the extension
  renews through its periodic keep-alive; a stale or throttled owned tab can otherwise stop
  renewing heartbeats. Reloading restarts the keep-alive. If it still disconnects after a minute,
  restart the desktop app so only one instance owns port 47831.

## Collection boundaries

- The app never reads or stores your X password. Authentication is completed manually in Opera
  GX, and the extension exchanges only a short-lived local pairing code with the desktop app.
- Each selected-profile run is capped at the chosen 5, 10, 20, or 30 qualifying posts.
- Selected-account runs retain their existing rules: only original posts qualify, with pinned
  posts, replies, reposts, quotes, other authors' cards, malformed links, and duplicate post IDs
  excluded.
- For You runs are capped at the chosen 10, 20, 30, or 50 items. Promoted content and non-post
  modules are excluded. Canonical replies are accepted, while quote and reply-parent detection is
  intentionally conservative.
- Existing posts deduplicate against the combined local library but remain observations in the
  latest captured For You order. A zero-result failed run does not replace the previous useful
  snapshot.
- X can change its markup or show a login challenge. A login challenge, rate limit, timeout,
  unsupported page, or lack of progress stops the run with a visible bounded explanation.
- The application contains no CAPTCHA bypass, login-wall bypass, automated login, or private API
  access. It never attempts to continue through login challenges or rate limits.
- Local photo files are requested through the app's internal gallery scheme; filesystem paths are
  not exposed in feed cards.

## Verification and optional live smoke test

Automated tests use deterministic fake collectors/providers and never contact live X. Run the
project checks with:

```powershell
.\.venv\Scripts\python.exe -m pytest
node --test extension/opera-xfeed/tests/*.test.mjs
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
git diff --check
```

An optional live smoke test is `python -m xfeed`. After reloading the version 4 unpacked extension
once:

1. Confirm **Opera connected / X signed in**.
2. Collect 10 For You items and confirm Opera visibly selects For You before extraction.
3. Collect 10 Following items and confirm Opera visibly selects Following before extraction.
4. Confirm each My Feed tab contains only its own current-session reader and older history.
5. Open Insights and reconcile Combined, For You, and Following counts with those readers.
6. Start a 50-item run, cancel it, and confirm already processed observations remain visible.
7. Collect one selected profile, then select two profile checkboxes and confirm their saved posts
   combine newest first.
8. Collect a new photo post from My Feed, a Source, and Manual Save. Confirm each local gallery
   appears after its photos finish saving, retains photo order, and falls back to the online embed
   while no local photo is available.
9. Close and relaunch. Confirm the prior nonempty session is available under **Past session** in
   Insights, both new current-session readers start empty, and unfinished photo work resumes.

Quick Save should remain responsive, and **Open X in Opera** must continue delegating
authentication to Opera GX rather than embedding a login view. Enter credentials only yourself;
never automate an authentication screen or attempt to bypass a CAPTCHA, login challenge, or rate
limit. If X shows a challenge, rate limit, changed-markup error, or exhausted timeline, record the
exact visible diagnostic and stop rather than bypassing it. Network and page behavior vary, so
this smoke test requires user interaction and is not part of automated verification.
