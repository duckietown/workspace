#!/bin/sh
set -eu

# Write the workspace image tag expected by Docker Compose interpolation
# based on the host architecture running this script.
devcontainer_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
env_file="$devcontainer_dir/.env"

# Docker image manifests use explicit Duckietown architecture suffixes.
case "$(uname -m)" in
  x86_64|amd64)
    workspace_image_tag="latest-amd64"
    ;;
  arm64|aarch64)
    workspace_image_tag="latest-arm64v8"
    ;;
  *)
    printf '%s\n' "Unsupported host architecture: $(uname -m). Expected amd64/x86_64 or arm64/aarch64." >&2
    exit 1
    ;;
esac

tmp_file=$(mktemp)
found_tag=0

# Preserve unrelated .env lines while replacing an existing image tag.
if [ -f "$env_file" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      WORKSPACE_IMAGE_TAG=*)
        printf 'WORKSPACE_IMAGE_TAG=%s\n' "$workspace_image_tag" >> "$tmp_file"
        found_tag=1
        ;;
      *)
        printf '%s\n' "$line" >> "$tmp_file"
        ;;
    esac
  done < "$env_file"
fi

# Append the setting for a first run or for a file that did not define it yet.
if [ "$found_tag" -eq 0 ]; then
  printf 'WORKSPACE_IMAGE_TAG=%s\n' "$workspace_image_tag" >> "$tmp_file"
fi

# Avoid rewriting the file when the selected tag is already current.
if [ ! -f "$env_file" ] || ! cmp -s "$tmp_file" "$env_file"; then
  mv "$tmp_file" "$env_file"
else
  rm "$tmp_file"
fi
