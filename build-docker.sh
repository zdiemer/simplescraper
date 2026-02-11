#!/bin/bash

IMAGE_NAME="simplescraper"
FULL_IMAGE_PATH="ghcr.io/zdiemer/$IMAGE_NAME"

# Get the current git branch for tagging
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD | sed 's/\//-/g')
# Get the short commit hash for unique versioning
GIT_SHA=$(git rev-parse --short HEAD)
# Use a timestamp for a "latest" build feel
TIMESTAMP=$(date +%Y%m%d-%H%M)

echo "--- Building Image: $IMAGE_NAME ---"

# 1. Build the image
# We tag it locally first for easy testing
docker build -t $IMAGE_NAME:latest .

echo "--- Tagging Image ---"

# 2. Tag for GHCR
# We create three tags:
# - latest: Always points to the newest build
# - branch: Useful for testing features (e.g., simplescraper:main)
# - sha: A permanent "immutable" version for rollbacks
docker tag $IMAGE_NAME:latest $FULL_IMAGE_PATH:latest
docker tag $IMAGE_NAME:latest $FULL_IMAGE_PATH:$GIT_BRANCH
docker tag $IMAGE_NAME:latest $FULL_IMAGE_PATH:$GIT_SHA-$TIMESTAMP

echo "--- Current Images ---"
docker images | grep $IMAGE_NAME

echo ""
read -p "Push to GHCR now? (y/n): " CONFIRM
if [[ "$CONFIRM" == "y" ]]; then
    echo "Pushing tags to ghcr.io..."
    docker push $FULL_IMAGE_PATH:latest
    docker push $FULL_IMAGE_PATH:$GIT_BRANCH
    docker push $FULL_IMAGE_PATH:$GIT_SHA-$TIMESTAMP
    echo "[SUCCESS] Images pushed to GHCR."
else
    echo "Push skipped. You can push later using: docker push $FULL_IMAGE_PATH"
fi