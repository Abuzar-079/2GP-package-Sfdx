#!/usr/bin/env python3
"""Add explicitly retrieved custom labels to the existing Salesforce source files.

Run the retrieve with --output-dir first, then pass that directory to this script.
Existing local label definitions and translations win on duplicate API names.
"""

from __future__ import annotations

import argparse
import copy
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct_text(element: ET.Element, wanted: str) -> str | None:
    for child in element:
        if local_name(child.tag) == wanted:
            return (child.text or "").strip()
    return None


def read_scope(manifest: Path) -> tuple[set[str], set[str]]:
    root = ET.parse(manifest).getroot()
    labels: set[str] = set()
    locales: set[str] = set()

    for types in root:
        if local_name(types.tag) != "types":
            continue
        metadata_type = direct_text(types, "name")
        members = {
            (child.text or "").strip()
            for child in types
            if local_name(child.tag) == "members" and (child.text or "").strip()
        }
        if metadata_type == "CustomLabel":
            labels.update(members)
        elif metadata_type == "Translations":
            locales.update(members)

    if not labels:
        raise SystemExit(f"No explicit CustomLabel members found in {manifest}")
    if "*" in labels or "*" in locales:
        raise SystemExit("Use explicit CustomLabel names and locale codes in the manifest")
    return labels, locales


def find_retrieved_file(root: Path, names: set[str]) -> Path | None:
    matches = sorted(path for path in root.rglob("*") if path.is_file() and path.name in names)
    if len(matches) > 1:
        joined = "\n  ".join(str(path) for path in matches)
        raise SystemExit(f"Found multiple retrieved copies for {sorted(names)}:\n  {joined}")
    return matches[0] if matches else None


def write_xml(path: Path, root: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else None
    if namespace:
        ET.register_namespace("", namespace)
    ET.indent(root, space="    ")

    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            ET.ElementTree(root).write(stream, encoding="UTF-8", xml_declaration=True)
        if path.exists():
            os.chmod(temporary_name, path.stat().st_mode)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def merge_records(
    incoming_path: Path,
    target_path: Path,
    *,
    collection: str,
    key_field: str,
    allowed_names: set[str],
) -> int:
    incoming_root = ET.parse(incoming_path).getroot()
    if target_path.exists():
        target_root = ET.parse(target_path).getroot()
        if local_name(target_root.tag) != local_name(incoming_root.tag):
            raise SystemExit(f"Unexpected XML root in {target_path}")
    else:
        target_root = ET.Element(incoming_root.tag, incoming_root.attrib)

    existing = {
        direct_text(record, key_field)
        for record in target_root
        if local_name(record.tag) == collection
    }
    added = 0
    for record in incoming_root:
        if local_name(record.tag) != collection:
            continue
        name = direct_text(record, key_field)
        if not name or name not in allowed_names or name in existing:
            continue
        target_root.append(copy.deepcopy(record))
        existing.add(name)
        added += 1

    if added:
        write_xml(target_path, target_root)
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieved-dir", required=True, type=Path)
    parser.add_argument("--manifest", default=Path("manifest/package.xml"), type=Path)
    parser.add_argument("--project-root", default=Path("."), type=Path)
    args = parser.parse_args()

    if not args.retrieved_dir.is_dir():
        raise SystemExit(f"Retrieve directory does not exist: {args.retrieved_dir}")

    labels, locales = read_scope(args.manifest)
    project_root = args.project_root
    added = 0

    label_source = find_retrieved_file(
        args.retrieved_dir,
        {"CustomLabels.labels-meta.xml", "CustomLabels.labels"},
    )
    if label_source:
        destination = project_root / "force-app/main/default/labels/CustomLabels.labels-meta.xml"
        count = merge_records(
            label_source,
            destination,
            collection="labels",
            key_field="fullName",
            allowed_names=labels,
        )
        added += count
        print(f"CustomLabels.labels: added {count}; kept existing local definitions")
    else:
        print("No retrieved CustomLabels file found; master labels were left unchanged")

    for locale in sorted(locales):
        source = find_retrieved_file(
            args.retrieved_dir,
            {f"{locale}.translation-meta.xml", f"{locale}.translation"},
        )
        if source is None:
            print(f"{locale}: no retrieved translation file; existing file was left unchanged")
            continue
        destination = project_root / f"force-app/main/default/translations/{locale}.translation-meta.xml"
        count = merge_records(
            source,
            destination,
            collection="customLabels",
            key_field="name",
            allowed_names=labels,
        )
        added += count
        print(f"{locale}: added {count} translations; kept existing local translations")

    if not added:
        print("No new label names were added; existing values were preserved")


if __name__ == "__main__":
    main()
