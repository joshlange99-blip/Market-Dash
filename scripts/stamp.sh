#!/bin/sh
# Bump the build stamp in index.html so open copies of the page reload to the new version.
sed -i "s/const BUILD = \"[0-9]*\"/const BUILD = \"$(date -u +%Y%m%d%H%M)\"/" "$(dirname "$0")/../index.html"
