# briq for Hermes

[![ci](https://github.com/briq-cloud/hermes-briq/actions/workflows/ci.yml/badge.svg)](https://github.com/briq-cloud/hermes-briq/actions/workflows/ci.yml)

Run Hermes' terminal, file and `execute_code` tools in a [briq](https://briq.cloud) sandbox: a microVM with its own disk, instead of your machine.

```bash
hermes plugins install briq-cloud/hermes-briq   # asks for BRIQ_API_KEY; installs briq-cloud
hermes plugins enable briq
hermes config set terminal.backend briq
```

Create a key on the [dashboard](https://app.briq.cloud). A sandbox sleeps when idle and wakes on the next command. A sleeping sandbox keeps its disk and costs nothing.

## Settings

| Key                  | Default                    | Meaning                                              |
| -------------------- | -------------------------- | ---------------------------------------------------- |
| `terminal.briq_size` | `medium`                   | `small`, `medium`, `large` or `xlarge`.              |
| `terminal.briq_image`| briq's default sandbox image | Any OCI image with `bash`. A per-task image wins. |
| `terminal.briq_sleep`| `1h`                       | Idle time before it sleeps: `30m`, `3h`, `never`.    |

`BRIQ_API_URL` points the plugin at another API (a self-hosted one). Each key also reads `TERMINAL_BRIQ_<KEY>` from the environment.

```bash
hermes config set terminal.briq_size large
hermes config set terminal.briq_image python:3.12
```

## How it behaves

- One sandbox per task. With `terminal.container_persistent: true` (the default) it is stopped when the session ends and started again for the same task, with its disk. With `false` it is destroyed.
- The working directory, exported variables and files persist across commands, as with every Hermes backend.
- The working directory starts in the sandbox's home. A host path in `terminal.cwd` is replaced by it.
- `~/.hermes` files that Hermes syncs to remote backends (skills, credential files you allow, cache) are copied into the sandbox and changed files are copied back at cleanup.
- Interrupting a command stops the sandbox (briq cannot cancel one call). The next command starts it again.
- A sandbox removed in the dashboard is replaced and Hermes tells the model the workspace was recreated.

## Not supported

- `container_cpu`, `container_memory` and `container_disk`: the size sets them.
- Forwarding host environment variables or mounting host paths. The sandbox gets neither.
- Bot Desktop and `computer_use` inside the sandbox (`supports_streams`): briq has no client that keeps stdin and stdout open.
- `hermes plugins isolation: host`. The plugin runs in process, as terminal backends must.

## What it does on your machine and in the cloud

- Calls the briq API (`https://api.briq.cloud`, or `BRIQ_API_URL`) with your key to create, stop, start and destroy a sandbox, run each command and move files. No other network call, no telemetry.
- Reads the key with Hermes' secret scope. It lists `BRIQ_API_KEY` in `strip_env_keys`, so a command the model writes cannot read it.
- Writes the id of each persistent sandbox (never the key) to `briq-sandboxes.json` under Hermes' sandbox directory.
- Commands and the files Hermes syncs leave your machine and run on briq. A sandbox has outbound network access and is billed to your briq team while it runs.
- A command stays in the sandbox if the connection drops. Stop or destroy a leftover sandbox on the dashboard.

## Develop

```bash
git clone https://github.com/briq-cloud/hermes-briq && cd hermes-briq
uv sync --group dev
uv run pytest                       # stand-ins for the Hermes modules
PYTHONPATH=/path/to/hermes-agent uv run pytest   # the same tests, plus a contract suite on real Hermes
hermes plugins validate . --install-deps
```

The tests mock HTTP, so no key is needed. Python 3.11 or later; the client is [`briq-cloud`](https://pypi.org/project/briq-cloud/).
