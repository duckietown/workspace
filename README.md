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

### Tests

The host identity tests use Python's standard library. Docker Compose is
also used to validate the actual mount configuration when available:

```shell
python3 -m unittest discover -s tests -v
```
