#!/bin/sh
# Repo-local git credential helper: serves the GitHub PAT from ../.env
# (gitignored) so pushes — including workflow files — authenticate with the
# right scopes. Wired via:  git config credential.helper <this file>
# If .env has no token, exits silently and git falls back to the keychain.
case "$1" in
  get)
    dir=$(cd "$(dirname "$0")/.." && pwd)
    token=$(grep '^GITHUB_TOKEN=' "$dir/.env" 2>/dev/null | cut -d= -f2-)
    [ -n "$token" ] || exit 0
    echo "username=itissanthoshkumar"
    echo "password=$token"
    ;;
esac
