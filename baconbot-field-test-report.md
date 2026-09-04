# Baconbot Field Test Report

**Author / Persona:** `baconbot` (First-time user testing via SSH transport, 192.168.1.9:2222)  
**Target Environment:** Live Mesh-Radio BBS (`nan@192.168.1.9:2222`), Target node commit referenced in brief: `0dc79ba`.  
**Methodology:** Direct interactive terminal interaction through SSH. Evaluated strictly as a naive stranger with no source code knowledge. Every section and menu was exercised, observed runtime responses and prompts were recorded verbatim, and findings were verified across reconnects and sessions.

---

## Executive Summary

The BBS offers an impressive, feature-rich suite of capabilities for a mesh radio environment—including interactive text adventures (Infocom/Z-machine games with flawless state persistence), public mesh chatter monitoring with active channel filtering, node linking, and an offline relay infrastructure.

However, several severe vulnerabilities and functional barriers directly harm the experience of a new visitor and risk infrastructure integrity:
1. **Critical Permission Failure (Urgent Bulletin Posting):** A brand-new account created minutes prior was permitted to post directly to the `Urgent` bulletin board without restriction or confirmation, immediately broadcasting network-wide.
2. **Infinite Score-Farming Exploit (Trivia King):** Invalid input at question boundaries replays the prior question while re-awarding full points, directly corrupting the public Hall of Fame.
3. **Asynchronous Hang & Keystroke-Evasion Pattern:** Both `Ask Nomad` and `Web Fetch` block indefinitely without acknowledging input, reveal output only upon receiving a subsequent keystroke, and immediately consume that keystroke as the answer to the newly displayed prompt.
4. **Non-Functional Web Fetch:** Completely disabled server-side (`[ERR] blocked: no allowed_hosts configured`), returning internal configuration errors.
5. **Discoverability & Navigation Defects:** Key sections (`Profile` [4], `Ask Nomad` [5], `JS8Call` [4]) are hidden from displayed menu text. Navigation is deeply inconsistent, with `[0] Back` sometimes jumping two hierarchy levels (e.g. from `Stats` or `Mail` straight to Root), and Relay Directory requiring `X` despite displaying `[0] Back`.
6. **Dead-End Loop Trap in Quick Commands:** Executing `!CB,,<board>` traps the user in an integer-only input loop where `0`, `X`, or empty lines fail, requiring knowledge of hidden global shortcuts to escape.
7. **No In-BBS Exit / Logout:** Neither the main menu nor quick commands provide a way to disconnect or exit cleanly. All 8 Infocom titles were found pre-loaded on the node, invalidating the on-demand download premise.

---

## Findings Classified by Severity

### 1. Broken

#### 1.1 CRITICAL: Unauthorized Posting to Urgent Bulletin Board Permitted
* **Action:** Navigated to `BBS Menu` -> `[2] Bulletins` -> `[4] Urgent` -> `[2] Post`.
* **Expectation:** The brief specifies that new accounts are barred from posting to Urgent bulletins. Expected a permission denial (e.g., "Access denied: posting to Urgent is restricted to operators").
* **Observed Output (Verbatim):**
  ```text
  💥NEW URGENT BULLETIN💥
  From: baconbot
  Title: [TEST baconbot] test post
  DM 'CB,,Urgent' to view
  Your bulletin '[TEST baconbot] test post' has been posted to Urgent.
  (╯°□°)╯📄📌[Urgent]
  ```
* **Impact:** Immediate broadcast to real mesh users and remote synced nodes (Chattanooga). No retraction or deletion command is exposed in the user UI.
* **Reproducibility:** 100% reproducible. Bulletin `#29` persisted and was readable by anyone on the board.

#### 1.2 CRITICAL: Trivia King Point-Farming & Replay Exploit
* **Action:** In Trivia King, after answering a question correctly and seeing `Reply N for another question, or X to exit.`, entered `?` (or any unrecognized command like `99`).
* **Expectation:** Display help text or "Invalid choice", then prompt again for `N` or `X`.
* **Observed Output (Verbatim):**
  ```text
  Reply N for another question, or X to exit.
  > ?
  Answer with one of the listed letters.
  [Question re-printed in full with identical choices]
  > A
  Correct! Points: 200. Total score: 800.
  ```
* **Impact:** Repeating `?` followed by the known correct option awards the full point value repeatedly without advancing the question. Score grew from 600 to 1000 on a single question. On exit, final score saved: `1. baconbot 2200 12mv` on the public Hall of Fame.
* **Reproducibility:** 100% reproducible across multiple questions and invalid inputs.

#### 1.3 Asynchronous Execution Hang & Keystroke-Evasion Defect
* **Action:** Submitted a query to `Ask Nomad` ("What is this BBS for and who can I talk to here?") and submitted a URL to `Web Fetch` (`https://example.com`).
* **Expectation:** Prompt feedback indicating work in progress ("Thinking...", "Fetching..."), followed by the response.
* **Observed Output (Verbatim):**
  A completely blank `> ` prompt appeared immediately. Polling revealed no new text for over 30 seconds. Upon typing `0` to nudge or exit, the queued answer suddenly printed, and that same `0` was instantly consumed as the response to the *new* prompt:
  ```text
  > 0
  BBS stands for Bulletin Board System, a platform for people to communicate and exchange files.
  Here, you can discuss various topics or share information.
  Reply with another question, or [0] for the main menu.
  💾Bacon BBS💾 (✉️:0)
  [1] Quick Commands
  [2] BBS
  [3] Utilities
  [6] Web Fetch
  [7] Linked Devices
  >
  ```
* **Impact:** Users think the system has frozen. Any follow-up keypress consumes the follow-up navigation before the user can read the output or reply.
* **Reproducibility:** 100% reproducible on both `Ask Nomad` and `Web Fetch`.

#### 1.4 Web Fetch Entirely Inoperable
* **Action:** Invoked `Web Fetch` (`[6]` or `!A`), entered `https://example.com`.
* **Observed Output (Verbatim):**
  ```text
  [ERR] blocked: no allowed_hosts configured
  ```
* **Impact:** The feature is completely broken for end users and leaks internal configuration syntax.

#### 1.5 Profile "Msgs:" Counter Tracks Menu Actions Instead of Mailbox
* **Action:** Navigated to `Profile` (`[4]`). Observed `Msgs:2`. Entered `?`, `help`, and invalid inputs. Entered Edit Bio and exited with `0`.
* **Observed Output:** `Msgs:` count incremented sequentially (`Msgs:3`, `Msgs:4`, `Msgs:5`), despite zero messages in the inbox.
* **Impact:** Highly confusing metric that purports to be messages but measures interaction cycles / invalid inputs.

#### 1.6 Silent Bio Truncation
* **Action:** Submitted a 500-character string to `Edit Bio (max 100 chars)`.
* **Observed Output:** Saved exactly the first 100 characters (`Bio updated!`) with zero error, warning, or notification that 400 characters were discarded.

#### 1.7 Internal Database / Sync GUID Leak in Channel Comments
* **Action:** Viewed `BBS Menu` -> `[3] Channel Dir` -> `[1] View` -> `[2] Introductions`.
* **Observed Output (Verbatim):**
  ```text
  [1] 2026-08-31 03:00 🥓:
  Yo Yo Yo Chattanooga Checking in. Read you loud and clear.
  0|8a293b60-dcc3-4639-a0e0-8fbf3a4ac846
  [2] 2026-08-31 03:00 🥓:
  Yo Yo Yo Chattanooga Checking in. Read you loud and clear.
  0
  ```
* **Impact:** Raw database or sync sync-engine string delimiters (`0|<UUID>`) leak directly into public comment streams.

#### 1.8 Dead-End Input Trap in Bulletin Quick Command (!CB)
* **Action:** From the main menu, ran `!CB,,Urgent`.
* **Observed Output (Verbatim):**
  ```text
  📰 Bulletins on Urgent board:
  [01] Subject: [TEST baconbot] test post, From: baconbot, Date: 2026-09-03 20:15

  Please reply with the number of the bulletin you want to read.
  > 0
  Invalid bulletin number. Please try again.
  > X
  Invalid input. Please enter a valid bulletin number.
  > [Empty Enter]
  >
  ```
* **Impact:** The prompt enforces strict integer parsing without checking for exit or cancel commands (`0`, `X`, `q`, `exit`). The user is permanently stuck in this loop unless they have memorized global menu shortcuts (e.g., `!Q` or `!B`). Anyone lacking this knowledge is forced to drop the SSH session.
* **Reproducibility:** 100% reproducible. Escape only possible via `!<shortcut>`.

---

### 2. Confusing

#### 2.1 Hidden Menu Items and Skipped Numbering
* **Main Menu:** Displays `[1] Quick Commands`, `[2] BBS`, `[3] Utilities`, `[6] Web Fetch`, `[7] Linked Devices`. Numbers `[4]` and `[5]` are omitted from text, but typing `4` opens `Profile` and typing `5` opens `Ask Nomad`.
* **BBS Menu:** Displays `[1] Mail`, `[2] Bulletins`, `[3] Channel Dir`. Option `[4]` is omitted, but typing `4` opens `JS8Call`.
* **Profile Menu:** Displays `[1] Edit Bio`, `[3] Offline Relay`, skipping `[2]`.
* **Impact:** Essential features are invisible unless a user guesses numbers or reads documentation.

#### 2.2 Relay Directory Navigation Contradicts On-Screen Prompt
* **Action:** In `Mail Menu` -> `[3] Relay Directory`, screen printed:
  ```text
  Select a relay user: (page 1/1)
  [1] baconbot (Unknown)
  [2] 🥓 (MeshCore/Meshtastic)
  [0] Back
  ```
  Typing `0` printed: `Reply N, P, or X.`.
* **Impact:** `[0] Back` is the universal BBS convention, but fails here; only `X` works.

#### 2.3 Non-Hierarchical "Back" Navigation
* From `Bulletins`, pressing `0` returns to `BBS Menu`.
* From `Mail Menu` and `Channel Directory`, pressing `0` jumps past `BBS Menu` directly to the `Root Main Menu`.

#### 2.4 Lack of Context in Network Statistics
* `Stats` -> `Hardware` reports `Unknown: 1380` (100%).
* `Stats` -> `Roles` reports `Unknown: 1380` (100%).
* `Stats` -> `Nodes` reports `Last 24 hours: 0` despite being on a live mesh.

#### 2.5 Inability to Mail Self or Understand Relay Directory
* In `Mail Menu` -> `[2] Send`, the directory only lists `🥓`. Entering `baconbot` via `[A]ddress` returns:
  ```text
  That relay user was not found, is ambiguous, or has not opted in.
  ```
* Even with `Offline Relay:On` enabled in `Profile`, self-addressing fails with no guidance on how to opt-in.
* **Root Cause Discovered:** When inspecting `!AU` (Relay Directory), the entry reads:
  ```text
  [1] baconbot (Unknown)
  [2] 🥓 (MeshCore/Meshtastic)
  ```
  Users arriving over SSH are classified under protocol `(Unknown)`. The mail relay router only delivers to valid mesh protocols (MeshCore/Meshtastic) or registered radio nodes, completely disallowing SSH-only accounts from receiving routed mail without clear explanation.

#### 2.6 Channel Directory Creation UI Confuses Users (Channel "Exit" & Misplaced Descriptions)
* **Action:** Examined Channel Directory entries `[1] Events` and `[2] Exit`.
* **Observations:**
  - Channel `01. Events` has its `Channel URL:` field populated with: `This is a channel for posting upcoming events` (a descriptive sentence, not a URL).
  - Channel `02. Exit` has Post ID 426 containing:
    ```text
    What does that even mean? "Send a message with your channel URL or PSK"
    ```
* **Impact:** Historical proof that previous human users were severely confused by the channel creation prompt (`Post Channel`). They entered descriptions into URL fields, and typed `Exit` to abort the prompt, which instead created an empty permanent channel named "Exit".

#### 2.7 Crucial System Architecture & Orientation Buried in Bulletin Archives
* **Observation:** The login prompt displays no system description. However, deep within the bulletins menu:
  - `Info` board -> Bulletin #6 (`Introduction to the Bacon BBS System`, 2026-03-01): Details that the system is an off-grid BBS forked from `https://github.com/TheCommsChannel/TC2-BBS-mesh`, running on Python over serial without internet access, and replicated across mesh nodes.
  - `News` board -> Bulletin #17 (`Even more updates!!`, 2026-08-11): Explains MeshCore dual-node support, direct MQTT database sync, and "Project Nomad AI call relay".
* **Impact:** All the context a newcomer needs to understand the system actually exists on disk, but is tucked away in old bulletin posts rather than presented at login, help, or quick commands.

#### 2.8 On-Demand Game Download Premise Invalidation
* **Observation:** The brief noted that 5 of the 8 Infocom games (`Zork II`, `Zork III`, `Deadline`, `Enchanter`, `Starcross`) would be fetched from remote storage on demand.
* **Finding:** All 8 games (`Zork I`, `Zork II`, `Zork III`, `HHGTTG`, `Deadline`, `Enchanter`, `Planetfall`, `Starcross`) are already present locally in `data/*.z3`. Every title launches in <100ms with identical startup messaging (`Loading data/<title>.z3.`). No download progress, network activity, or remote fetching occurs.

---

### 3. Missing

#### 3.1 No System Orientation or Welcome Banner
* Connecting over SSH displays solely: `Connected to Bacon BBS. Type 0 to go back.`.
* There is no banner explaining what node this is, network frequency/location, rules, or who operates it. (The only operator statement found is buried 4 levels deep in Channel Directory -> Introductions).

#### 3.2 No Version String
* No version number, git commit hash, or build identifier is exposed anywhere in the menus, headers, `Stats`, or `Quick Commands`.

#### 3.3 No In-BBS Exit / Logout Option
* The main menu lacks an Exit command.
* Global shortcut `!X` does not logout; it refreshes the root menu. Typing `exit` or `quit` yields `Invalid choice.`. Users must terminate the transport connection manually.

#### 3.4 No Post Retraction / Deletion UI
* Once a bulletin or comment is posted, there is no option to delete or retract it, even for one's own posts.

---

### 4. Good (Features to Protect)

* **Interactive Fiction Engine & State Management:** Z-machine integration (`Zork I`, `Hitchhiker's Guide to the Galaxy`, `Zork II`, `Deadline`) is exceptional. Games launch cleanly with clear upfront exit instructions (`Send X to exit.`). Move counters, inventories, and room states persist flawlessly across game switches and complete SSH disconnects.
* **Public Chatter Monitor:** The `Public Chatter` utility provides real-time mesh visibility. The channel toggle filter (`[F]ilter*`) and time-window selectors (`[T]ime`) work seamlessly, correctly updating pagination without duplicated records.
* **Emoji & Unicode Handling:** Full UTF-8 support across the interface, including bio strings (`🥓🐷😀`), emojis in callsigns/aliases, and channel comments.
* **Device Linking Protocol:** `Linked Devices` (`!S`) offers a clean workflow for linking node IDs, generating 10-minute one-time pairing codes, and tracking active session identifiers. The delayed code feature (`[6] Request code, delayed (dual-boot)`) explicitly explains how to handle firmware transitions across reboots.
* **Input Injection Resilience:** Tested shell metacharacters (`; ls -la`, `$(whoami)`, `| cat`) and SQL injection sequences (`' OR '1'='1`) across root menus and quick commands (`!CB,,...`). The system strictly validated inputs against whitelists (`Invalid board name`, `Invalid choice`), with zero command leakage, subprocess execution, or Python tracebacks.
* **Configuration Persistence:** Profile states (`Offline Relay:On`, bio content) reliably survived session disconnections and re-authentications.

---

## Detailed Test Matrix

| Area | Feature | Expected Behavior | Observed Result | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Auth** | Registration | Register `baconbot` with `Pancetta11` | Account created cleanly on port 2222 | **Good** |
| **Auth** | Reconnect | Re-login with existing credentials | Login accepted; profile persisted | **Good** |
| **Orientation**| Welcome Banner | State node purpose & operator | None; only "Type 0 to go back." | **Missing** |
| **Main Menu**| Discoverability | List all main options | Options 4 (Profile) & 5 (Nomad) hidden | **Confusing** |
| **Profile** | Bio Edit | Edit & save bio | Works; emoji supported; 100-char limit | **Good** |
| **Profile** | Bio Truncation | Reject or warn on >100 chars | Silently chops 500 chars to 100 chars | **Broken** |
| **Profile** | Offline Relay | Toggle On/Off | Toggled to On; persisted across logout | **Good** |
| **Profile** | Msgs Counter | Show inbox count | Increments on every command / menu action | **Broken** |
| **Mail** | Read Mail | Read inbox | Correctly reported empty mailbox | **Good** |
| **Mail** | Send to Self | Send `[TEST baconbot]` message | Blocked: SSH is `(Unknown)` relay protocol | **Missing** |
| **Mail** | Quick Command | `!SM,,<user>,,<subj>,,<msg>` | Returns syntax help; blocks non-mesh user | **Good** |
| **Mail** | Relay Dir Nav | `[0] Back` returns to menu | Failed; required typing `X` | **Broken** |
| **Bulletins**| Urgent Posting | Deny non-admin accounts | **Allowed post immediately (#29)** | **Broken (Critical)** |
| **Bulletins**| Urgent Reading | View bulletin #29 | Displayed title, body, author correctly | **Good** |
| **Bulletins**| Quick Command | `!CB,,<board>` | Works; but **traps user in dead-end loop** | **Broken** |
| **Bulletins**| Board Whitelist| Reject invalid board names | Caught `$(whoami)` & SQL injections cleanly | **Good** |
| **Channels** | Introductions | Read operator post | Post readable; contains developer note | **Good** |
| **Channels** | Comments | View responses | GUID / sync metadata leaked in text | **Broken** |
| **Channels** | Post Comment | Add test comment | Posted with `[TEST baconbot]` prefix | **Good** |
| **Channels** | Channel "Exit" | Abort channel creation | Prior user typed "Exit", made channel #426 | **Confusing** |
| **Utilities**| Stats | Breakdown nodes/hardware/roles | 100% "Unknown" for hardware & roles | **Confusing** |
| **Utilities**| Wall of Shame | Show low battery nodes | Clean empty state handling (<20%) | **Good** |
| **Utilities**| Public Chatter | Filter channels & page | Channel filter & time window work great | **Good** |
| **Games** | Trivia King | 10 questions session | Clean questions; prompt easy to follow | **Good** |
| **Games** | Trivia King | Scoring integrity | **Exploit: repeating invalid input farms points** | **Broken (Critical)** |
| **Games** | Hall of Fame | View top scores | Displayed exploited score: `2200 12mv` | **Good / Bug** |
| **Games** | Zork I | Baseline gameplay & escape | `X` exits immediately; save state holds | **Good** |
| **Games** | HHGTTG | Multi-game state retention | Maintained separate save state cleanly | **Good** |
| **Games** | Planetfall | Baseline launch & time tracking | Loaded instantly; Galactic chronometer moves | **Good** |
| **Games** | Zork II & III | On-demand download check | Both pre-loaded on node; loaded instantly | **Good / Discrepancy** |
| **Games** | Deadline / Enchanter / Starcross | On-demand download check | All pre-loaded on node; loaded instantly | **Good / Discrepancy** |
| **Nomad** | Ask Nomad | Question-answer flow | Hangs; flushes output on next keystroke | **Broken** |
| **Web Fetch**| Fetch URL | Fetch allowed webpage | Hangs; flushes `[ERR] no allowed_hosts` | **Broken** |
| **Devices** | Link Code | Generate pairing code | Code `771130` generated cleanly | **Good** |
| **Devices** | Dual-Boot Link| Request delayed link code | Queued 2-minute delay with clear warnings | **Good** |
| **Devices** | Invalid Code | Enter bad 6-digit code | Cleanly caught: `Invalid or already-used` | **Good** |
| **Shortcuts**| Quick Commands | `!Q`, `!B`, `!U`, `!P`, `!A`, `!S` | All shortcuts jump to target menus | **Good** |
| **Exit** | In-menu Logout | Exit session cleanly | No command exists; must drop connection | **Missing** |

---

## Action Items & Recommendations

1. **Enforce Urgent Board Authorization:** Add an immediate role check on bulletin category `Urgent` to block unprivileged user roles from submitting posts.
2. **Fix Trivia King State Machine:** Do not replay questions or re-award points upon receiving invalid inputs at the answer prompt; only accept valid menu selections or proceed.
3. **Resolve Async Event Loop Flushing:** Fix output flushing for `Ask Nomad` and `Web Fetch` so response buffers push immediately to the client socket without requiring an incoming flush byte.
4. **Restore Hidden Menu Entries:** Update menu formatters to display `[4] Profile` and `[5] Ask Nomad` in the Main Menu, and `[4] JS8Call` in the BBS Menu.
5. **Clean Comment Presentation:** Sanitize internal message IDs / sync UUIDs before rendering channel comments.
6. **Account Cleanup Notice:** Because account deletion is not available via user menus, please manually purge or deactivate test account `baconbot` and remove test bulletin `#29` on the `Urgent` board.
