#!/usr/bin/env bash
# Does gnome-shell's own parser accept these files?
#
# Run this before every login that is meant to pick up an edit. An extension
# that does not parse is not "degraded" -- gnome-shell refuses to import it, and
# that takes out screen capture, window control and the pointer together, with
# no way back except another logout.
#
# WHY NOT node --check
#
# It does not work. On 2026-08-22 a block comment containing `*/` (written as
# `/proc/*/exe`) closed itself, turning the rest of the sentence into code
# inside a method body. `node --check` passed it. `node --no-lazy --check`
# passed it too. Node only pre-parses at the top level, and the error was inside
# a method; a simplified top-level reproduction WAS caught, which is exactly
# what made the gate look trustworthy right up until the next login.
#
# gjs is the engine gnome-shell runs. Ask it.
#
#   ./check-syntax.sh                       # every .js beside this script
#   ./check-syntax.sh path/to/one.js ...    # just these
set -uo pipefail

if ! command -v gjs >/dev/null; then
    echo "gjs is not installed; cannot check what gnome-shell would say" >&2
    exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -gt 0 ]; then
    files=("$@")
else
    mapfile -t files < <(find "$here" -name '*.js' ! -name '*.bak*' | sort)
fi

status=0
for file in "${files[@]}"; do
    # gjs -m parses first, then executes. Outside gnome-shell the imports fail
    # with ImportError, which is expected and means the file parsed. Only a
    # SyntaxError is a real failure here.
    output="$(gjs -m "$file" 2>&1)"
    if grep -q 'SyntaxError' <<<"$output"; then
        printf 'FAIL  %s\n' "$file"
        grep -o 'SyntaxError.*' <<<"$output" | head -1 | sed 's/^/        /'
        status=1
    else
        printf 'ok    %s\n' "$file"
    fi
done

if [ "$status" -ne 0 ]; then
    echo
    echo "Do NOT log out until this is clean: gnome-shell will refuse to import" >&2
    echo "the extension, and recovering costs another logout." >&2
fi
exit "$status"
