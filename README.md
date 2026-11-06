# Duckietown Workspaces

Duckietown Workspaces provide a ready-to-use development environment for
Duckietown.

The workspace runs inside a dev container and includes the Duckietown Shell (`dts`),
Docker-in-Docker for virtual robots, Python/Jupyter tooling, and a browser-based
desktop for GUI tools.

Dev container initialization automatically starts or refreshes the native host
runner using the scripts in the active `.devcontainer` directory. A `.devcontainer`
directory copied into another development tree is an independent snapshot:
rebuilding from an older copy does not fetch this repository's updated scripts.
