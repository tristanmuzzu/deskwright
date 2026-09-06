#!/usr/bin/env python3
"""Independent saved-file oracle; does not perform the editing task."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
fixtures = Path(__file__).resolve().parent / 'fixtures'

def read_text(path):
    # UTF-8 BOM and platform line endings are not task failures.
    return path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').rstrip('\n')

result = {
    'output_exists': args.output.is_file(),
    'source_unchanged': args.source.read_bytes() == (fixtures / 'project-brief.txt').read_bytes(),
    'matches_requested_revision': False,
    'reopen_verified': 'must be observed in application; a file check cannot prove this',
}
if result['output_exists']:
    result['matches_requested_revision'] = read_text(args.output) == read_text(fixtures / 'expected-revision.txt')
    result['output_sha256'] = hashlib.sha256(args.output.read_bytes()).hexdigest()
result['file_checks_passed'] = all(result[k] for k in ('output_exists', 'source_unchanged', 'matches_requested_revision'))
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['file_checks_passed'] else 1)
