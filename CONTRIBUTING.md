# Contributing

Two owners are collaborating from macOS and Windows, with coding assistants. Contributions should be small enough that both people can understand, run, and explain them.

## Get the context

Read [README.md](README.md), [docs/STATUS.md](docs/STATUS.md), [plan.md](plan.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Coding assistants must also read [AGENTS.md](AGENTS.md); Claude has [CLAUDE.md](CLAUDE.md) as an entry point.

The repository contains documentation and, under `backend/`, the Phase 0 backend foundation (setup in [backend/README.md](backend/README.md)). No dependencies or hardware drivers need installing just to review the documents. The phased plan in `plan.md` is approved; implement only what an owner explicitly authorizes. `backend/` belongs to Harshal and `dashboard/` and `firmware/` to Dev; the two sides meet through written contracts such as [docs/DASHBOARD_API.md](docs/DASHBOARD_API.md).

## Shared Git workflow

Anyone can clone the public repository. Direct push access requires a repository collaborator invitation; someone without access can fork the repository and open a pull request. Public visibility alone does not grant write access.

```sh
git clone https://github.com/DevMehta13/adaptive-self-healing-network.git
cd adaptive-self-healing-network
```

For a new contribution, first check for unfinished local changes. Then update `main` and create a descriptive branch:

```sh
git status
git switch main
git pull --ff-only
git switch -c docs/clarify-network-demo
```

Agree on ownership when work overlaps. Before committing, review the changes and stage only the intended files. Use a small descriptive commit, push the branch, and open a pull request for the other contributor to review. Do not force-push shared branches or overwrite a teammate's changes to resolve a conflict.

A pull request should explain the problem or requested change, resulting behavior, actual checks, and remaining limitations. Update the shared context in the same contribution when relevant.

## Working with an assistant

A useful starting request is:

> Read AGENTS.md and the linked project context. Check the current code and working tree. Work only on [specific task]. Explain the relevant networking concept simply, verify the change, and update docs/STATUS.md with what actually happened.

Describe the concrete task instead of asking an agent to build everything. Before handing work to the other person, make sure committed status and design documents reflect the current result. Chat history on one laptop is not shared context on the other.

## macOS and Windows

The intended toolchain is portable; PlatformIO documents support for both [macOS and Windows](https://docs.platformio.org/en/latest/core/installation/). This does not mean the eventual project has already been tested on either platform.

- Keep serial-port selection configurable: macOS often uses `/dev/cu.*`; Windows uses names such as `COM3`.
- Use data-capable USB cables for programming and the gateway. Identify the board's actual USB interface before choosing a driver.
- Use portable path handling, match filename capitalization exactly, and avoid checked-in absolute paths.
- When dependencies are introduced, record tested versions and setup instructions for both systems. Keep machine-specific environments out of Git.
- One laptop connects to C for a physical integration run. The simulator is planned for independent software work but does not exist yet.

## Verification expectations

Documentation changes need consistency and link checks. Firmware changes eventually need a build for the selected ESP8266 board and relevant hardware observations. Routing/protocol changes need meaningful tests for the changed behavior, including relevant failure cases. Dashboard data should agree with controller evidence.

Label results as simulated or hardware-based. Record the tested operating system, devices, and setup where relevant. If hardware or another platform is unavailable, say so and leave that validation pending.

## Shared context ownership

| File | Update it when |
| --- | --- |
| `README.md` | The project's public description or real setup entry point changes |
| `plan.md` | Owners approve a scope or priority change |
| `docs/ARCHITECTURE.md` | A technical decision is agreed, implemented, or disproved |
| `docs/STATUS.md` | Meaningful work finishes or an unresolved issue changes |
| `AGENTS.md` | Shared agent working rules need adjustment |
| `CLAUDE.md` | The shared context entry points move |

Do not store credentials, personal access tokens, private conversations, or real device identifiers in example configuration. Use clearly fictitious examples and local configuration for hardware-specific values.
