#!/usr/bin/env python3
"""
Checker for unused style entries in Telegram Desktop .style files.

Parses .style files in --definitions dirs to extract top-level variable
definitions, then searches all .cpp, .h, .mm and .style files in --search
dirs for references. Reports (and optionally removes) entries that appear
to be unused.

Usage:
  python3 check_unused_styles.py \
      --definitions <dir>... \
      --search <dir>... \
      [--exclude <dir>...] \
      [--root <dir>] \
      [--remove] [--commit]

Modes:
  --remove   Rewrite .style files with unused entries deleted.
  --commit   Implies --remove; additionally `git add` + `git commit` the
             modified files in the current working directory.

Notes:
  --exclude only filters where DEFINITIONS are scanned (and removed from).
  References are always searched across the full --search tree, so cross-
  module usages stay visible. Typical use: pass submodule paths as
  --exclude to avoid editing files owned by another repository.

Output:
  Prints a per-file list of unused entries plus a machine-readable summary
  line `[summary] removed=N` (N is the total number of removed entries).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from collections import defaultdict


CPP_EXTS = {".cpp", ".h", ".mm"}
STYLE_EXT = ".style"

# Marker in a comment immediately above or on the same line as a style entry
# keeps that entry from being reported/removed even with no detected refs.
PROTECTION_MARKER = "[[maybe_unused]]"


def find_files(dirs, extensions, exclude=None):
    """Yield files with given extensions under dirs, pruning exclude paths."""
    excluded = {Path(e).resolve() for e in (exclude or [])}
    for d in dirs:
        d = Path(d)
        if not d.exists():
            continue
        for root, subdirs, files in os.walk(d):
            root_path = Path(root).resolve()
            subdirs[:] = [
                sd for sd in subdirs
                if (root_path / sd).resolve() not in excluded
            ]
            for f in files:
                fp = Path(root) / f
                if fp.suffix in extensions:
                    yield fp


def find_style_files(dirs, exclude=None):
    return list(find_files(dirs, {STYLE_EXT}, exclude=exclude))


def is_entry_protected(raw_lines, def_idx):
    """
    Return True if the definition at raw_lines[def_idx] (0-based) is marked
    with PROTECTION_MARKER either on its own line or on any preceding run of
    blank/comment lines. Stops at the first non-comment, non-blank line so
    a marker on an earlier entry does not leak to a later one.
    """
    if PROTECTION_MARKER in raw_lines[def_idx]:
        return True
    i = def_idx - 1
    while i >= 0:
        line = raw_lines[i]
        stripped = line.strip()
        if stripped == '':
            i -= 1
            continue
        is_commentish = (
            stripped.startswith('//')
            or stripped.startswith('/*')
            or stripped.startswith('*')
            or stripped.endswith('*/'))
        if not is_commentish:
            break
        if PROTECTION_MARKER in line:
            return True
        i -= 1
    return False


def parse_style_definitions(style_file):
    """
    Parse a .style file and extract top-level variable definitions.

    Returns list of (name, line_number, protected) tuples.

    Rules:
    - Struct TYPE definitions: `TypeName {` (PascalCase followed by {) -> skip
    - Variable definitions: `name: ...` at top level (not inside braces) -> collect
    - `using` directives -> skip
    - Comments (// and /* */) -> skip
    - Entries marked with PROTECTION_MARKER in a nearby comment -> protected flag set
    """
    text = style_file.read_text(encoding="utf-8", errors="replace")
    raw_lines = text.split('\n')
    definitions = []

    text_no_block = re.sub(
        r'/\*.*?\*/', lambda m: '\n' * m.group().count('\n'),
        text, flags=re.DOTALL)

    lines = text_no_block.split('\n')
    brace_depth = 0

    for lineno_0, stripped_source in enumerate(lines):
        lineno = lineno_0 + 1

        line = re.sub(r'//.*', '', stripped_source).strip()
        if not line:
            continue

        if brace_depth == 0:
            if line.startswith('using '):
                continue

            m_struct = re.match(r'^([A-Z][A-Za-z0-9]*)\s*\{', line)
            if m_struct:
                brace_depth += line.count('{') - line.count('}')
                continue

            m_var = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:', line)
            if m_var:
                name = m_var.group(1)
                protected = is_entry_protected(raw_lines, lineno_0)
                definitions.append((name, lineno, protected))

        brace_depth += line.count('{') - line.count('}')
        if brace_depth < 0:
            brace_depth = 0

    return definitions


def collect_all_definitions(style_files):
    """
    Returns (defs, protected_names):
      defs            — dict name -> list of (file, line) where defined
      protected_names — set of names with at least one definition marked
                        with PROTECTION_MARKER (removal is suppressed for them).
    """
    defs = defaultdict(list)
    protected_names = set()
    for sf in style_files:
        for name, lineno, protected in parse_style_definitions(sf):
            defs[name].append((sf, lineno))
            if protected:
                protected_names.add(name)
    return defs, protected_names


def search_cpp_references(dirs, names_set):
    """Search C++ files for `st::name` references."""
    referenced = set()
    pattern = re.compile(r'\bst::([a-zA-Z_][a-zA-Z0-9_]*)\b')

    for fp in find_files(dirs, CPP_EXTS):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            name = m.group(1)
            if name in names_set:
                referenced.add(name)

    return referenced


def search_style_references(style_files, names_set):
    """Search .style files for cross-references to defined variables."""
    referenced = set()
    ident_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b')

    for sf in style_files:
        try:
            text = sf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)

        for line in text.split('\n'):
            line = re.sub(r'//.*', '', line).strip()
            if not line or line.startswith('using '):
                continue

            m_def = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:', line)
            if m_def:
                after_colon = line[m_def.end():]
                for m in ident_pattern.finditer(after_colon):
                    name = m.group(1)
                    if name in names_set:
                        referenced.add(name)
            else:
                for m in ident_pattern.finditer(line):
                    name = m.group(1)
                    if name in names_set:
                        referenced.add(name)

            for name in re.findall(r'\(([a-zA-Z_][a-zA-Z0-9_]*)\)', line):
                if name in names_set:
                    referenced.add(name)

    return referenced


def find_entry_line_range(filepath, start_line):
    """
    Given a file and the starting line of a top-level definition,
    return (first_line, last_line) — 1-based, inclusive.
    """
    lines = filepath.read_text(encoding="utf-8", errors="replace").split('\n')
    idx = start_line - 1

    def strip_comments(s):
        return re.sub(r'//.*', '', s)

    first = idx
    cleaned = strip_comments(lines[idx])

    opens = cleaned.count('{')
    closes = cleaned.count('}')
    if opens == closes:
        last = idx
    else:
        depth = opens - closes
        last = idx
        for j in range(idx + 1, len(lines)):
            c = strip_comments(lines[j])
            depth += c.count('{') - c.count('}')
            last = j
            if depth <= 0:
                break

    return first + 1, last + 1


def remove_unused_entries(definitions, unused_names):
    """Remove unused entries from .style files. Returns dict file -> count."""
    by_file = defaultdict(list)
    for name in unused_names:
        for sf, lineno in definitions[name]:
            first, last = find_entry_line_range(sf, lineno)
            by_file[sf].append((first, last, name))

    modified = {}
    for sf, ranges in by_file.items():
        lines = sf.read_text(encoding="utf-8", errors="replace").split('\n')

        to_remove = set()
        for first, last, _ in ranges:
            for i in range(first - 1, last):
                to_remove.add(i)

        new_lines = [l for i, l in enumerate(lines) if i not in to_remove]

        cleaned = []
        for line in new_lines:
            if line.strip() == '' and cleaned and cleaned[-1].strip() == '':
                continue
            cleaned.append(line)

        sf.write_text('\n'.join(cleaned), encoding="utf-8")
        modified[sf] = len(ranges)

    return modified


def relpath(p, root):
    try:
        return p.relative_to(root)
    except ValueError:
        return p


def analyze(definitions_dirs, search_dirs, exclude_dirs):
    """Returns (definitions, unused, n_def_files, n_cpp, n_style, n_protected)."""
    definition_files = find_style_files(definitions_dirs, exclude=exclude_dirs)
    definitions, protected_names = collect_all_definitions(definition_files)
    names_set = set(definitions.keys())

    search_style = find_style_files(search_dirs)
    cpp_refs = search_cpp_references(search_dirs, names_set)
    style_refs = search_style_references(search_style, names_set)

    unused = names_set - (cpp_refs | style_refs | protected_names)
    return (definitions, unused, len(definition_files),
            len(cpp_refs), len(style_refs), len(protected_names))


def main():
    parser = argparse.ArgumentParser(
        description="Find (and optionally remove) unused .style entries.")
    parser.add_argument(
        "--definitions", nargs="+", required=True, type=Path,
        help="Directories whose .style files define candidate names "
             "(and from which unused entries will be removed).")
    parser.add_argument(
        "--search", nargs="+", required=True, type=Path,
        help="Directories to search for references to candidate names "
             "(scans .cpp/.h/.mm and .style files).")
    parser.add_argument(
        "--exclude", nargs="*", default=[], type=Path,
        help="Directories to exclude when scanning for definitions "
             "(typically submodule paths).")
    parser.add_argument(
        "--root", type=Path, default=Path("."),
        help="Root for displaying relative paths in output.")
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove unused entries from .style files.")
    parser.add_argument(
        "--commit", action="store_true",
        help="Remove + commit (implies --remove). Runs `git commit` in CWD.")
    args = parser.parse_args()

    if args.commit:
        args.remove = True

    definitions_dirs = [d.resolve() for d in args.definitions]
    search_dirs = [d.resolve() for d in args.search]
    exclude_dirs = [d.resolve() for d in args.exclude]
    root = args.root.resolve()

    print("Configuration:")
    print(f"  definitions: {[str(d) for d in definitions_dirs]}")
    print(f"  search:      {[str(d) for d in search_dirs]}")
    print(f"  exclude:     {[str(d) for d in exclude_dirs]}")
    print()

    print("Scanning .style files for definitions...")
    definitions, unused, n_def, n_cpp, n_style, n_protected = analyze(
        definitions_dirs, search_dirs, exclude_dirs)
    print(f"  Found {n_def} .style files for definitions")
    print(f"  Found {len(definitions)} top-level variable definitions")
    print(f"  Found {n_cpp} names referenced in C++")
    print(f"  Found {n_style} names referenced in .style files")
    if n_protected > 0:
        print(f"  {n_protected} definitions marked {PROTECTION_MARKER} (skipped)")

    print(f"\n{'='*70}")
    print(f"UNUSED STYLE ENTRIES: {len(unused)}")
    print(f"{'='*70}\n")

    if not unused:
        print("All style entries appear to be used!")
        print("[summary] removed=0")
        return 0

    by_file = defaultdict(list)
    for name in sorted(unused):
        for sf, lineno in definitions[name]:
            by_file[str(relpath(sf, root))].append((lineno, name))

    for filepath in sorted(by_file.keys()):
        print(f"\n{filepath}:")
        for lineno, name in sorted(by_file[filepath]):
            print(f"  line {lineno:>5}: {name}")

    print(f"\nTotal: {len(unused)} potentially unused entries "
          f"across {len(by_file)} files")

    if not args.remove:
        print("[summary] removed=0")
        return 1

    all_modified = defaultdict(int)
    total = 0
    pass_num = 0

    while unused:
        pass_num += 1
        print(f"\nPass {pass_num}: removing {len(unused)} unused entries...")
        modified = remove_unused_entries(definitions, unused)
        for sf, count in sorted(modified.items(), key=lambda x: str(x[0])):
            print(f"  {relpath(sf, root)}: removed {count}")
            all_modified[sf] += count
        total += sum(modified.values())

        definitions, unused, *_ = analyze(
            definitions_dirs, search_dirs, exclude_dirs)

    print(f"\nDone. Removed {total} entries from {len(all_modified)} files "
          f"in {pass_num} pass(es).")
    print(f"[summary] removed={total}")

    if not args.commit:
        return 0

    print("\nCommitting...")
    paths = [str(f) for f in all_modified.keys()]
    subprocess.run(["git", "add"] + paths, check=True)
    msg = f"Removed {total} unused style entries."
    subprocess.run(["git", "commit", "-m", msg], check=True)
    print("Committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
