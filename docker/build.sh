#!/bin/sh
# Build the image with the version stamped in.
#
# The container has no .git and no git binary, so version_info cannot resolve
# the commit count or hash at runtime the way it does on a normal install.
# Passing them as build args is what keeps `docker run baconbs` from reporting
# the same frozen version for every build ever made.
set -eu

cd "$(dirname "$0")/.."

IMAGE="${1:-baconbs:local}"

BUILD_NUMBER="$(git rev-list --count HEAD 2>/dev/null || echo '')"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo '')"

if [ -z "$BUILD_NUMBER" ]; then
    echo "WARNING: not a git checkout, so the image will report a fallback version." >&2
fi

echo "Building $IMAGE (build ${BUILD_NUMBER:-unknown}, commit ${GIT_COMMIT:-unknown})"
exec docker build \
    -f docker/Dockerfile \
    --build-arg "BBS_BUILD_NUMBER=$BUILD_NUMBER" \
    --build-arg "BBS_GIT_COMMIT=$GIT_COMMIT" \
    -t "$IMAGE" \
    .
