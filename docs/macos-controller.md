# Protected controller on macOS

`scripts/install-controller-macos.py` stages launchd daemon and watchdog files,
copies a standalone runtime, and deploys under the hidden `_researchcompute`
service identity. Code and configuration are root-owned; the key and ledger
belong to the service. The socket authenticates the client's actual UID and
accepts only the configured Mac user. Agents receive the ordinary unprivileged
`research-compute` client, never the service credential.

Select CBAI in the Runpod console and create a dedicated `aniket-controller` API
key with pod-management Read/Write permissions. In a human terminal run:

```sh
sudo /usr/bin/python3 scripts/store-controller-key.py
```

The hidden prompt writes `/etc/research-compute/runpod.env` mode 600. The key
does not enter a command, terminal output, or shell history. Existing keys are
never overwritten. The installer changes its owner to the service identity.

Prepare a self-contained Python 3.11+ distribution, Node 20+ executable, and a
directory containing `node_modules/@runpod/mcp-server@4.0.0`. Dependencies are
copied into the protected prefix, with links dereferenced; never point a service
at binaries in the agent's writable home. Package installation is separate and
must not use the controller key. Then deploy:

```sh
sudo python3 scripts/install-controller-macos.py \
  --config config.macos.example.json --client-user aniket \
  --python-tree PYTHON_DISTRIBUTION --node NODE_EXECUTABLE \
  --provider-tree PREPARED_NPM_DIRECTORY --enable
./install.sh --agents codex claude
```

Use `--stage-root /tmp/controller-preview` without `--enable` for an offline
preview; it never creates users or starts services. Actual deployment refuses
registered services and conflicting configuration rather than overwriting a
running installation. Disable both agents' direct Runpod MCP connections once
the controller works, so lifecycle requests cannot bypass its ownership checks.
The official skills can remain as reference material.

The macOS example uses session budgets instead of a cumulative account cap and
allows any GPU/image/template. Its generous resource ceilings are operational
limits; the requested dollar amount governs admission for each investigation.
Storage is retained and keeps billing separately. The Mac must remain awake and
online: a sleeping or failed host cannot stop remote GPUs, and this is not a
provider-enforced billing cap. Resume after an outage by reconciling the ledger
before admitting new work. No pod is ever automatically terminated.

The prefix is checked against the current provider name before each start/stop.
Concurrent changes outside the controller between a check and an API action are
outside this client-side enforcement. Host administrators retain access, and
agents with separate provider credentials could bypass the service. Do not give
those credentials to managed research sessions.
