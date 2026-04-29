#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(tr -d '[:space:]' < VERSION)"
TAG="v$VERSION"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must use semantic versioning like 0.1.0" >&2
  exit 1
fi

if ! grep -q "^## \[$VERSION\]" CHANGELOG.md; then
  echo "Add a CHANGELOG entry for version $VERSION before creating a tag." >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Git working tree must be clean before creating a release tag." >&2
  exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG already exists." >&2
  exit 1
fi

git tag -a "$TAG" -m "Release $TAG"

echo "Created tag $TAG"
echo "Push it with:"
echo "  git push origin main $TAG"
