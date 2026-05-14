#!/bin/bash
set -e

# Mirror dev container feature installation inside the custom workspace image.
# Each upstream installer is downloaded explicitly because this Docker build
# path does not rely on the `devcontainer` CLI to inject features for us.
echo "Installing dev container features..."

# Install only the common tools required before handing off to feature scripts.
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    sudo

# Feature installers and the running workspace both expect an ubuntu user.
if ! id -u ubuntu > /dev/null 2>&1; then
    useradd -m -s /bin/bash ubuntu
fi

# Keep sudo passwordless so VS Code dev container operations can elevate.
echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu
chmod 0440 /etc/sudoers.d/ubuntu

# Fail early if the image cannot grant sudo to the workspace user.
if [ ! -f /etc/sudoers.d/ubuntu ]; then
    echo "ERROR: Failed to create sudoers file for ubuntu user"
    exit 1
fi

USERNAME=ubuntu
INSTALL_PATH=/tmp/features
# Pin installer scripts to immutable upstream commits so workspace image builds
# do not drift when feature repositories update their main branches.
DEVCONTAINERS_FEATURES_REF="71c999dff6218c6905de7b7a55167fba7eb5709a"
TAILSCALE_CODESPACE_REF="44307894cef3057589a9b79f25ea1770682a050e"
DEVCONTAINERS_FEATURES_BASE_URL="https://raw.githubusercontent.com/devcontainers/features/${DEVCONTAINERS_FEATURES_REF}/src"
TAILSCALE_CODESPACE_BASE_URL="https://raw.githubusercontent.com/tailscale/codespace/${TAILSCALE_CODESPACE_REF}/src/tailscale"

# Use a short-lived staging directory for downloaded installer scripts.
mkdir -p ${INSTALL_PATH}

# Feature installers read their options from environment variables.
echo "Installing git feature..."
curl -fsSL "${DEVCONTAINERS_FEATURES_BASE_URL}/git/install.sh" -o ${INSTALL_PATH}/git-install.sh
chmod +x ${INSTALL_PATH}/git-install.sh
VERSION="os-provided" ${INSTALL_PATH}/git-install.sh

# Download and install git-lfs feature
echo "Installing git-lfs feature..."
curl -fsSL "${DEVCONTAINERS_FEATURES_BASE_URL}/git-lfs/install.sh" -o ${INSTALL_PATH}/git-lfs-install.sh
chmod +x ${INSTALL_PATH}/git-lfs-install.sh
VERSION="latest" ${INSTALL_PATH}/git-lfs-install.sh

# Download and install docker-in-docker feature
# The workspace relies on nested Docker for virtual Duckietown robots.
echo "Installing docker-in-docker feature..."
curl -fsSL "${DEVCONTAINERS_FEATURES_BASE_URL}/docker-in-docker/install.sh" -o ${INSTALL_PATH}/docker-install.sh
chmod +x ${INSTALL_PATH}/docker-install.sh
VERSION="28.5.2" \
DOCKERDASHCOMPOSEVERSION="v2" \
MOBY="true" \
ENABLENONROOTDOCKER="true" \
USERNAME=${USERNAME} \
${INSTALL_PATH}/docker-install.sh

# Download and install tailscale feature
# Tailscale's dev container feature expects its entrypoint helpers nearby.
echo "Installing tailscale feature..."
curl -fsSL "${TAILSCALE_CODESPACE_BASE_URL}/install.sh" -o ${INSTALL_PATH}/tailscale-install.sh
curl -fsSL "${TAILSCALE_CODESPACE_BASE_URL}/tailscaled-entrypoint.sh" -o ${INSTALL_PATH}/tailscaled-entrypoint.sh
curl -fsSL "${TAILSCALE_CODESPACE_BASE_URL}/tailscaled-devcontainer-start.sh" -o ${INSTALL_PATH}/tailscaled-devcontainer-start.sh
chmod +x ${INSTALL_PATH}/tailscale-install.sh
VERSION="latest" ${INSTALL_PATH}/tailscale-install.sh

# Download and install desktop-lite feature
# noVNC gives the workspace a browser-accessible desktop on port 6080.
echo "Installing desktop-lite feature..."
curl -fsSL "${DEVCONTAINERS_FEATURES_BASE_URL}/desktop-lite/install.sh" -o ${INSTALL_PATH}/desktop-install.sh
chmod +x ${INSTALL_PATH}/desktop-install.sh
PASSWORD="noPassword" \
WEBPORT="6080" \
VNCPORT="5901" \
NOVNCVERSION="1.3.0" \
INSTALL_NOVNC="true" \
USERNAME=${USERNAME} \
${INSTALL_PATH}/desktop-install.sh

# Remove installer scripts after they have been baked into the image layer.
rm -rf ${INSTALL_PATH}

echo "Feature installation complete!"
