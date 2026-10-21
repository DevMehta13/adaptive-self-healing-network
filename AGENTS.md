# Instructions for coding agents

These instructions apply throughout this repository. They provide shared context for Codex, Claude, and other assistants. Follow the user's current request; do not infer approval for the entire proposed system from the documentation.

## Read before working

1. [README.md](README.md) — purpose and current project state.
2. [docs/STATUS.md](docs/STATUS.md) — what exists and pending decisions.
3. [plan.md](plan.md) — approved software-first phases and completion checkpoints.
4. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design proposals and technical limits.
5. [CONTRIBUTING.md](CONTRIBUTING.md) — collaboration and platform expectations.

Read the relevant code and check the working tree before making changes. Treat these documents as repository context, not proof that described behavior has been implemented. If sources conflict, check code and history, explain the conflict, and resolve only what the current task authorizes.

## Project constraints

- Target **four ESP32-S NodeMCU-style boards** (PlatformIO board `esp32dev`, Arduino-ESP32 2.x). The project moved from ESP8266 to ESP32-S on 2026-10-21 at the owner's direction; ESP8266 results in older notes are historical. Do not require extra hardware without discussing the cost and reason.
- A is the source; B and D are relays; C is the destination and USB gateway. The laptop computes routes.
- Prefer ₹800 hardware spend and target below ₹1,000. No complete bill of materials has been verified.
- Support macOS and Windows. Avoid hardcoded device paths and platform-specific scripts where portable alternatives suffice.
- Keep computation and visualization on the laptop. Keep node memory, message sizes, retries, and caches bounded.
- AI must not be required for routing, failure detection, or the offline demonstration.
- The approved phased plan lives in `plan.md`. Do not create a separate `ROADMAP.md` or `docs/ROADMAP.md` unless requested. Plan approval is not implementation authorization; wait for the owner's explicit instruction to begin.

## How to contribute

- For Dev's work on `dev-work`, explain each phase before starting it: its purpose, what will be built, prerequisites, concepts to learn, and the observable completion checks. Work through the approved increments one at a time and stop after each with results and the next step. Preserve this teaching-first workflow across sessions.
- Implement the specific requested step in a small, working increment. Do not generate the full system from a broad idea or add unsolicited advanced features.
- Explain the networking concept in simple language, what changes, why it matters, and how to observe it. The owners are beginners who must defend the project during evaluation.
- Distinguish confirmed decisions, proposals, assumptions, and measured results. Ask focused questions only when an unresolved choice materially changes the task.
- Prefer simple, inspectable implementations and existing conventions. Do not adopt a mesh library that hides the intended custom routing without discussing the change.
- Preserve other contributors' changes. Avoid unrelated rewrites and destructive git operations. Do not commit secrets or device-specific local configuration.
- When behavior is implemented, add meaningful verification proportional to the change. Use radio hardware for radio claims; a simulator does not prove physical delivery.
- Report failed or unavailable checks plainly. Never claim macOS, Windows, or hardware testing that did not occur.

## Technical integrity

- Management messages must have a way around a broken data route. Do not rely solely on the failed forwarding entries to distribute their replacements.
- Enforce configured logical links for management and data; label this topology honestly in demos.
- Keep planned routes separate from observed packet paths. Animations are not delivery evidence.
- A radio callback reporting success is not an end-to-end application acknowledgement.
- Handle duplicates, sequence-number reuse after restart, stale topology, unreachable destinations, and bounded retries explicitly when implementing those behaviors.
- RTT is not one-way delay; clocks on different boards are not assumed synchronized.
- Verify ESP32 ESP-NOW and Arduino-ESP32 APIs against the pinned core; ESP-NOW callbacks run in the Wi-Fi task, not in `loop()`. Never fabricate peer RSSI or substitute access-point RSSI.
- C failure and complete network partitions are not covered by the initial healing guarantee. During controller loss, nodes may use only confirmed, controller-installed backup next hops; they must not calculate routes themselves.
- Do not promise zero packet loss, a fixed recovery time, or a route through B regardless of policy.

## Keep context synchronized

- Update `docs/STATUS.md` after meaningful work with what changed, actual verification, unresolved issues, and the next sensible task.
- Update architecture or scope when an approved decision changes them; avoid duplicating details across every document.
- Add accurate setup/run/test instructions as runnable components appear. Pin shared dependencies then.
- Keep `CLAUDE.md` as a thin entry point to these shared instructions, so the two agents do not receive divergent rules.
- End a contribution with a short explanation of the result, verification, limitations, and next step. Commit/push according to the task's authorization and the contribution workflow.
