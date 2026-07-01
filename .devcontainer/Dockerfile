# Use Ubuntu 24.04 LTS as the base image
FROM ubuntu:24.04

# Set environment variables for non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive

# Copy the feature installation script to /tmp
COPY scripts/install-features.sh /tmp/install-features.sh

# Copy the other scripts to /usr/local/bin
COPY scripts/startup-script.sh /usr/local/bin/startup-script.sh

COPY scripts/host_runner_bootstrap.py /usr/local/bin/host_runner_bootstrap.py

COPY scripts/configure_avahi.sh /usr/local/bin/configure_avahi.sh

COPY scripts/configure_avahi.awk /usr/local/bin/configure_avahi.awk

COPY scripts/mdns_republisher.py /usr/local/bin/mdns_republisher.py

COPY scripts/mdns-republisher-supervisor.sh /usr/local/bin/mdns-republisher-supervisor.sh

# Make the scripts executable
RUN chmod +x \
    /tmp/install-features.sh \
    /usr/local/bin/startup-script.sh \
    /usr/local/bin/configure_avahi.sh \
    /usr/local/bin/mdns-republisher-supervisor.sh && \
# Run the feature installation script
    /tmp/install-features.sh && \
# Add Mozilla PPA for Firefox
    apt-get update && \
    apt-get install -y --no-install-recommends software-properties-common && \
    add-apt-repository -y ppa:mozillateam/ppa && \
    echo "Package: *\nPin: release o=LP-PPA-mozillateam\nPin-Priority: 1001" > /etc/apt/preferences.d/mozilla-firefox && \
# Install additional packages
    apt-get update && \
    apt-get install -y --no-install-recommends --allow-downgrades \
        # Networking
        avahi-utils \
        dnsutils \
        iproute2 \
        iputils-ping \
        libnss-mdns \
        libnss3-tools \
        ssh \
        # General tools
        build-essential \
        bumpversion \
        direnv \
        libfuse2 \
        nano \
        pipx \
        python3-dev \
        python3-full \
        rsync \
        unzip \
        wget \
        # Desktop applications
        firefox && \
# Configure mDNS for fast .local resolution
    sed -i "s/^hosts:.*/hosts: files mdns4_minimal [SUCCESS=return] mdns6_minimal [SUCCESS=return] dns/" /etc/nsswitch.conf && \
# Clean up
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/*

# Switch to the ubuntu user
USER ubuntu

# Set the working directory
WORKDIR /home/ubuntu

# Install the Duckietown Shell as the ubuntu user
RUN pipx install duckietown-shell && \
    pipx ensurepath
