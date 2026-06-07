#!/usr/bin/env bash
# Fails the commit if any PII pattern slips into the repo.
# Wire as a pre-commit hook:  ln -sf ../../scripts/check_anonymity.sh .git/hooks/pre-commit
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
# The forbidden tokens are stored base64-encoded so this script file itself
# never contains the literal strings it grep-rejects.
patterns="$(echo 'KGNsZW1lbnR8ZmlndWVpcmVkb3x0ZWNobm9vQGxpdmVcLmZyfGRlXC5maWd1ZWlyZWRvKQ==' | base64 -d)"

# Only scan staged contents (so the hook fires on `git commit`).
# When run manually, fall back to scanning the working tree.
if git diff --cached --name-only --diff-filter=ACMR | grep -q .; then
    files=$(git diff --cached --name-only --diff-filter=ACMR)
    # shellcheck disable=SC2086
    if echo "$files" | xargs -d '\n' grep -lEi "$patterns" 2>/dev/null; then
        echo "PII detected in staged changes; aborting commit." >&2
        exit 1
    fi
else
    if grep -rEi --exclude-dir=.git --exclude-dir=__pycache__ "$patterns" "$repo_root"; then
        echo "PII detected in working tree; aborting." >&2
        exit 1
    fi
fi

# Also verify the local repo identity is the anonymous one.
expected_email="31561379+technoo10201@users.noreply.github.com"
actual_email=$(git -C "$repo_root" config --get user.email || echo "")
if [[ "$actual_email" != "$expected_email" ]]; then
    echo "Refusing to commit: user.email is '$actual_email', expected '$expected_email'." >&2
    echo "Run: git -C $repo_root config user.email '$expected_email'" >&2
    exit 1
fi

exit 0
