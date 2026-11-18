#!/usr/bin/env python3
"""Detect personal details and private workspace files before publication."""

import argparse
import os
from pathlib import Path
import re
import sys


FORBIDDEN_NAMES = {'agents.md', 'claude.md'}
RULES = {
    'email address': re.compile(
        rb'(?<![a-z0-9._%+\-])'
        rb'(?!user@(?:api\.spotify\.com|open\.spotify\.com|i\.scdn\.co)/)'
        rb'[a-z0-9._%+\-]+@(?!users\.noreply\.github\.com\b)'
        rb'[a-z0-9.-]+\.[a-z]{2,}\b',
        re.IGNORECASE,
    ),
    'personal home path': re.compile(
        rb'(?:/(?:home|users)/|[a-z]:\\users\\)[a-z0-9_.\-]+', re.IGNORECASE,
    ),
    'private host address': re.compile(
        rb'\b(?:forge-\d+|pop[-]os|[a-z0-9][a-z0-9.\-]*\.ts\.net'
        rb'|[a-z0-9-]+\.(?:local|lan|internal))\b',
        re.IGNORECASE,
    ),
}


def check_text(data):
    for line_number, line in enumerate(data.splitlines(), start=1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                yield f'{rule} on line {line_number}'


def check_tree(root):
    def inaccessible(error):
        raise error

    for directory, folders, files in os.walk(root, onerror=inaccessible):
        folders[:] = [name for name in sorted(folders) if name.casefold() != '.git']
        for name in [*folders, *sorted(files)]:
            path = Path(directory) / name
            relative = path.relative_to(root)
            if name.casefold() == '.git':
                continue
            if name.casefold() in FORBIDDEN_NAMES:
                yield f'{relative}: private instruction filename'
            if path.is_symlink():
                yield f'{relative}: symbolic link'
                continue
            for issue in check_text(os.fsencode(name)):
                yield f'{relative.parent}: {issue} in filename'
            if name not in folders:
                for issue in check_text(path.read_bytes()):
                    yield f'{relative}: {issue}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path.cwd())
    root = parser.parse_args().root
    findings = list(check_tree(root))
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        return 1
    print('Public tree privacy checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
