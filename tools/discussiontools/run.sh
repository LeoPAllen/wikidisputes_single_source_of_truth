#!/bin/sh
set -eu

image="${DISCUSSIONTOOLS_IMAGE:-wikidisputes-discussiontools:rel1_46-pinned}"
exec docker run --rm -i \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --tmpfs=/tmp:rw,noexec,nosuid,size=64m \
  "$image" "$@"
