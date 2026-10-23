# Duckietown Workspaces

Duckietown Workspaces provide a ready-to-use development environment for
Duckietown.

The workspace runs inside a dev container and includes the Duckietown Shell (`dts`),
Docker-in-Docker for virtual robots, Python/Jupyter tooling, and a browser-based
desktop for GUI tools.

## One computer for the host and its Workspaces

The same Duckietown account can use DTS on the host and inside its local
Workspaces without consuming a second computer registration.

During initialization, the [bootstrap helper](.devcontainer/scripts/host_runner_bootstrap.py)
reads the host OS identity used by DTS: the Linux machine ID, macOS platform
UUID, or native Windows MachineGuid. It validates and normalizes the UUID
and writes the generated, Git-ignored `.devcontainer/.host-machine-id`
file. Every initialization reads the actual host identity again; a copied
or stale generated file is not used as the identity source.

The [Compose configuration](.devcontainer/docker-compose.yml) mounts this
file read-only at both `/etc/machine-id` and `/var/lib/dbus/machine-id`.
DTS and native Linux Renderers therefore compute the same computer
fingerprint as host DTS. Browser Renderers use that same DTS identity.
Rebuilding the Workspace does not create another registered computer, and
different hosts still have different identities.

Host DTS does not need to be installed for identity initialization.
Host and Workspace profiles, identity tokens, credentials, and permission
caches remain separate; configure each DTS installation normally. No
additional Hub device slot, container exemption, or DTS identity override
is introduced. The [DTS plan, grace-period, transfer, and offline rules](https://github.com/duckietown/duckietown-shell#plan-and-computer-management)
are unchanged.

Initialize the Workspace from the same host OS where host DTS runs. If
initialization runs inside a separate VM or WSL distribution, that
environment is the host for identity purposes; it does not automatically
inherit the outer macOS or native Windows identity. This preserves
existing OS-based computer registrations.

An invalid or unavailable host identity stops initialization instead of
generating a random ID or silently reusing a stale one. Missing bind
sources also fail rather than creating an empty machine-ID directory.
The unchanged identity file is not replaced, so restarting initialization
does not invalidate mounts in already-running Workspaces.

This uses DTS's existing OS-identity trust model, not hardware attestation.
Administrator-controlled OS identities can still be cloned or spoofed.
Do not commit or distribute the generated machine-ID file or share
identity tokens and permission caches.

### Updating an existing Workspace

After updating this checkout, use __Dev Containers: Rebuild Container__
in VS Code to apply shared computer registration. Reopening an existing container
without recreating it does not add them.

If the account is already registered on this host, the updated Workspace
uses that registration without a transfer. If it was registered using an
older container-specific identity, run `dts plan transfer` once from the
updated Workspace or matching host DTS. Normal transfer waiting periods
still apply; initialization does not automatically transfer or revoke
existing permissions.

## Native Viewer host delegation

Duckietown Viewer commands on Ente authorize in the launching DTS before starting the backend
or delegating a desktop frontend. The native host receives a private
`DTS_VIEWER_HANDOFF` path through the existing shared host-runner directory,
not an identity token. The wire path is relative to that directory and resolved
on the native host, so host/guest absolute paths need not match. Traversal,
escaping links and non-desktop handoff files are rejected.
Its native verifier checks the same live DTS session,
selected app, exact backend URL, component inventory and host computer.
A forwarded frontend URL or host-runner flag alone is not authorization.

Update the signed command sets used by both active profiles and Viewer-capable
native DTS on both host and guest; older renderer-only builds cannot consume
Viewer protocol 1. A verified Ente handoff stays on the guarded path even if the
native profile otherwise selects Daffy. Keep the
launching Workspace/DTS process alive. The backend and frontend renew
ten-second heartbeats and stop protected operation when their bounded proofs
expire, at most thirty seconds. The shared host identity still consumes only
one Ente computer registration.

Restart/reinitialize an existing native host-runner process with the updated
Workspace scripts so it loads the relative-handoff mapping too.

The private handoff directory must be readable on both sides through the
configured shared path, and the backend/broker address must be reachable from
the relevant Docker/native networks. Do not manually copy handoffs, forward
DT2 tokens or use a bare URL to work around routing errors. On expiry or
delegation failure, relaunch through DTS. Daffy retains its legacy plan-free
Viewer path.

Source/custom Viewer work requires its separate staff/superuser developer grant;
see the
[DTS Viewer guide](https://github.com/duckietown/duckietown-shell/blob/main/devel.md#duckietown-viewer-authorization).
Real WSL/native Windows, FEX, macOS LaunchServices and host/Docker networking
validation remains a release gate, not a claim established by unit tests.

## Tests

The host identity tests use Python's standard library. Docker Compose is
also used to validate the actual mount configuration when available:

```shell
python3 -m unittest discover -s tests -v
```
