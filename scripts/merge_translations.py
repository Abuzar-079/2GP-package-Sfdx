#!/usr/bin/env python3
"""Merge Salesforce Translations files entry by entry instead of overwriting them.

Why this exists
---------------
`sf project retrieve start` writes a retrieved Translations file (fr, de, ...) over the
whole local file. The org only returns translation entries for components that were in
the same retrieve, so every other entry in the local file (older labels, tabs, other
packages' entries) is lost. The same file names also exist in every force-app-* folder.

This script merges entries by (type, name) instead:
  * an entry that already exists in the target is UPDATED when the source has a real,
    different value (a placeholder `<!-- ... -->` never overwrites a real value);
  * an entry that does not exist is ADDED when its component is in scope
    (--labels / --tabs) and the source value is not a placeholder;
  * every other target entry is KEPT unchanged.
Files that are already sorted by name stay sorted; otherwise new entries are appended
to their group. Untouched files are not rewritten.

Safe workflow for the Base package (force-app)
----------------------------------------------
  python3 scripts/merge_translations.py manifest \
      --labels force-app/main/default/labels/CustomLabels.labels-meta.xml \
      --locales ar,da,de,en_US,es,fr,it,ja,ko,nl_NL,sv,zh_CN \
      --out /tmp/tr-manifest.xml
  sf project retrieve start --manifest /tmp/tr-manifest.xml \
      --target-metadata-dir /tmp/tr-retrieve --unzip --target-org 2GP
  python3 scripts/merge_translations.py merge --source /tmp/tr-retrieve \
      --target force-app/main/default/translations \
      --labels force-app/main/default/labels/CustomLabels.labels-meta.xml \
      --types customLabels

Never retrieve Translations straight into a package folder.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

MD_NS = "http://soap.sforce.com/2006/04/metadata"
NS = "{" + MD_NS + "}"
ENTITIES = {'"': "&quot;", "'": "&apos;"}
TRANSLATION_SUFFIXES = (".translation-meta.xml", ".translation")
TAB_SUFFIXES = (".tab-meta.xml", ".tab")


# --------------------------------------------------------------------------- parsing

def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_with_comments(path: Path) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(path, parser=parser).getroot()


def child(record: ET.Element, name: str) -> ET.Element | None:
    for element in record:
        if isinstance(element.tag, str) and local(element.tag) == name:
            return element
    return None


def record_name(record: ET.Element) -> str | None:
    element = child(record, "name")
    return (element.text or "").strip() if element is not None else None


def label_value(record: ET.Element) -> str | None:
    """Return the translated text, or None when the entry is an untranslated placeholder."""
    element = child(record, "label")
    if element is None:
        return None
    text = element.text or ""
    if not text.strip() and any(c.tag is ET.Comment for c in element):
        return None
    return text


def is_simple(record: ET.Element) -> bool:
    """True for entries made of <label> and <name> only (customLabels, customTabs, ...)."""
    names = [local(c.tag) for c in record if isinstance(c.tag, str)]
    return sorted(names) == ["label", "name"]


def locale_of(path: Path) -> str:
    for suffix in TRANSLATION_SUFFIXES:
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    raise ValueError(path)


def find_translation_files(root: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if path.is_file() and path.name.endswith(TRANSLATION_SUFFIXES):
            found.setdefault(locale_of(path), []).append(path)
    return found


def read_label_names(path: Path) -> set[str]:
    root = ET.parse(path).getroot()
    return {
        (child(label, "fullName").text or "").strip()
        for label in root
        if isinstance(label.tag, str) and local(label.tag) == "labels" and child(label, "fullName") is not None
    }


def read_tab_names(directory: Path) -> set[str]:
    names = set()
    for path in directory.rglob("*"):
        for suffix in TAB_SUFFIXES:
            if path.is_file() and path.name.endswith(suffix):
                names.add(path.name[: -len(suffix)])
    return names


# --------------------------------------------------------------------------- writing
# Same layout Salesforce uses: 4-space indent, &quot;/&apos; escaped, placeholders kept
# as <label><!-- text --></label>, trailing newline.

def render_text(element: ET.Element) -> str:
    out = escape(element.text or "", ENTITIES)
    for sub in element:
        if sub.tag is ET.Comment:
            out += f"<!--{sub.text}-->"
        else:
            raise ValueError(f"Unexpected nested element <{local(sub.tag)}>")
        out += escape(sub.tail or "", ENTITIES)
    return out


def render_element(element: ET.Element, depth: int) -> list[str]:
    pad = "    " * depth
    tag = local(element.tag)
    kids = [c for c in element if isinstance(c.tag, str)]
    if not kids:
        return [f"{pad}<{tag}>{render_text(element)}</{tag}>"]
    lines = [f"{pad}<{tag}>"]
    for kid in kids:
        lines.extend(render_element(kid, depth + 1))
    lines.append(f"{pad}</{tag}>")
    return lines


def render(root: ET.Element) -> bytes:
    records = [r for r in root if isinstance(r.tag, str)]
    head = '<?xml version="1.0" encoding="UTF-8"?>\n'
    if not records:
        return (head + f'<Translations xmlns="{MD_NS}"/>\n').encode("utf-8")
    lines = [f'<Translations xmlns="{MD_NS}">']
    for record in records:
        lines.extend(render_element(record, 1))
    lines.append("</Translations>")
    return (head + "\n".join(lines) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- merge

def make_record(type_name: str, name: str, value: str) -> ET.Element:
    record = ET.Element(NS + type_name)
    ET.SubElement(record, NS + "label").text = value
    ET.SubElement(record, NS + "name").text = name
    return record


def set_value(record: ET.Element, value: str) -> None:
    label = child(record, "label")
    for sub in list(label):
        label.remove(sub)
    label.text = value


def merge_locale(target_root, sources, *, labels, tabs, types, add_only, log, locale):
    records = [r for r in target_root if isinstance(r.tag, str)]
    index = {(local(r.tag), record_name(r)): r for r in records}
    groups: dict[str, list[ET.Element]] = {}
    for record in records:
        groups.setdefault(local(record.tag), []).append(record)
    was_sorted = {
        t: [record_name(r) for r in g] == sorted(record_name(r) for r in g) for t, g in groups.items()
    }

    stats = dict(updated=0, added=0, out_of_scope=0, placeholder=0, conflict=0)
    incoming: dict[tuple[str, str], tuple[str, Path]] = {}
    for source in sources:
        for record in parse_with_comments(source):
            if not isinstance(record.tag, str) or not is_simple(record):
                continue
            key = (local(record.tag), record_name(record))
            if types and key[0] not in types:
                continue
            value = label_value(record)
            if value is None:
                stats["placeholder"] += 1
                continue
            if key in incoming and incoming[key][0] != value:
                stats["conflict"] += 1
                log.append((locale, *key, "conflict", incoming[key][0], value, str(source)))
            incoming[key] = (value, source)

    new_records: dict[str, list[ET.Element]] = {}
    for key, (value, source) in incoming.items():
        type_name, name = key
        existing = index.get(key)
        if existing is not None:
            if not is_simple(existing) or add_only:
                continue
            old = label_value(existing)
            if old != value:
                set_value(existing, value)
                stats["updated"] += 1
                log.append((locale, type_name, name, "updated", old if old is not None else "<placeholder>", value, str(source)))
            continue
        if (type_name == "customLabels" and labels is not None and name not in labels) or (
            type_name == "customTabs" and tabs is not None and name not in tabs
        ):
            stats["out_of_scope"] += 1
            continue
        new_records.setdefault(type_name, []).append(make_record(type_name, name, value))
        stats["added"] += 1
        log.append((locale, type_name, name, "added", "", value, str(source)))

    if new_records:
        ordered: list[ET.Element] = []
        for type_name in sorted(set(groups) | set(new_records)):
            group = groups.get(type_name, []) + new_records.get(type_name, [])
            if was_sorted.get(type_name, True):
                group.sort(key=record_name)
            ordered.extend(group)
        for record in records:
            target_root.remove(record)
        target_root.extend(ordered)
    return stats


def cmd_merge(args) -> int:
    labels = set().union(*(read_label_names(p) for p in args.labels)) if args.labels else None
    tabs = set().union(*(read_tab_names(p) for p in args.tabs)) if args.tabs else None
    types = set(args.types.split(",")) if args.types else None
    wanted = set(args.locales.split(",")) if args.locales else None
    skipped = set(args.skip_locales.split(",")) if args.skip_locales else set()

    by_locale: dict[str, list[Path]] = {}
    for source in args.source:
        for locale, paths in find_translation_files(source).items():
            by_locale.setdefault(locale, []).extend(paths)

    log: list[tuple] = []
    for locale in sorted(by_locale):
        if (wanted and locale not in wanted) or locale in skipped:
            continue
        target = args.target / f"{locale}.translation-meta.xml"
        if target.exists():
            original = target.read_bytes()
            root = parse_with_comments(target)
            if render(root) != original:
                print(f"{locale}: {target} is not in standard Salesforce layout; it will be normalised", file=sys.stderr)
        elif args.create_missing:
            original, root = None, ET.Element(NS + "Translations")
        else:
            print(f"{locale}: no target file (use --create-missing to create it); skipped")
            continue
        stats = merge_locale(
            root, by_locale[locale], labels=labels, tabs=tabs, types=types,
            add_only=args.add_only, log=log, locale=locale,
        )
        data = render(root)
        changed = data != original
        if changed and not args.dry_run:
            atomic_write(target, data)
        total = sum(1 for r in root if isinstance(r.tag, str))
        print(
            f"{locale}: updated {stats['updated']}, added {stats['added']}, total {total}"
            f" | skipped: out of scope {stats['out_of_scope']}, placeholders {stats['placeholder']}"
            + (f", source conflicts {stats['conflict']}" if stats["conflict"] else "")
            + ("" if changed else " | unchanged")
            + (" (dry run)" if args.dry_run and changed else "")
        )

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["locale", "type", "name", "action", "old", "new", "source"])
            writer.writerows(log)
        print(f"Change log written to {args.report}")
    return 0


def cmd_manifest(args) -> int:
    names = sorted(set().union(*(read_label_names(p) for p in args.labels)))
    locales = sorted(args.locales.split(","))
    if len(names) + len(locales) > 10000:
        raise SystemExit(f"{len(names)} labels exceed the 10,000-component retrieve limit; split the labels file")

    def block(type_name, members):
        return "    <types>\n" + "".join(f"        <members>{escape(m)}</members>\n" for m in members) + f"        <name>{type_name}</name>\n    </types>\n"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Package xmlns="{MD_NS}">\n'
        + block("CustomLabel", names)
        + block("Translations", locales)
        + f"    <version>{args.api_version}</version>\n</Package>\n"
    )
    args.out.write_text(xml, encoding="utf-8")
    print(f"Wrote {args.out}: {len(names)} labels, {len(locales)} locales")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    merge = sub.add_parser("merge", help="merge source translation files into a translations folder")
    merge.add_argument("--source", type=Path, action="append", required=True,
                       help="retrieved folder/file or package translations folder (repeatable)")
    merge.add_argument("--target", type=Path, required=True, help="translations folder to update")
    merge.add_argument("--labels", type=Path, action="append",
                       help="CustomLabels file; only these labels are ADDED (repeatable)")
    merge.add_argument("--tabs", type=Path, action="append",
                       help="folder with *.tab(-meta.xml); only these tabs are ADDED (repeatable)")
    merge.add_argument("--types", help="comma list of entry types to touch, e.g. customLabels")
    merge.add_argument("--locales", help="comma list of locales to process (default: all found)")
    merge.add_argument("--skip-locales", help="comma list of locales to leave alone")
    merge.add_argument("--add-only", action="store_true", help="never change existing values")
    merge.add_argument("--create-missing", action="store_true", help="create target files that do not exist")
    merge.add_argument("--dry-run", action="store_true")
    merge.add_argument("--report", type=Path, help="write a CSV of every added/updated entry")
    merge.set_defaults(func=cmd_merge)

    manifest = sub.add_parser("manifest", help="write a package.xml for labels + their translations")
    manifest.add_argument("--labels", type=Path, action="append", required=True)
    manifest.add_argument("--locales", required=True)
    manifest.add_argument("--out", type=Path, required=True)
    manifest.add_argument("--api-version", default="68.0")
    manifest.set_defaults(func=cmd_manifest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
