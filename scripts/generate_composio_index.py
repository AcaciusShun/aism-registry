#!/usr/bin/env python3
"""Generate an aism-compatible registry index from the Composio skills repository."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO_URL = "https://github.com/ComposioHQ/awesome-claude-skills.git"
DEFAULT_REF = "master"
DEFAULT_COLLECTION_ROOTS = ["composio-skills"]
DEFAULT_SOURCE = "composiohq"
DEFAULT_TARGETS = ["claude"]
DEFAULT_VERSION = "0.1.0"


@dataclass(frozen=True)
class SkillEntry:
    slug: str
    name: str
    description: str
    version: str
    repo: str
    path: str
    ref: str
    source: str
    targets: list[str]
    folder: str

    def to_index_record(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "name": self.name,
            "repo": self.repo,
            "path": self.path,
            "ref": self.ref,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "targets": self.targets,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate index.json from the Composio awesome-claude-skills repository."
    )
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Upstream git repository URL.")
    parser.add_argument("--ref", default=DEFAULT_REF, help="Git branch, tag, or ref to clone.")
    parser.add_argument(
        "--collection-root",
        action="append",
        dest="collection_roots",
        default=None,
        help="Relative directory inside the upstream repo whose direct children are skill folders. Repeatable.",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Registry source identifier written into each entry.",
    )
    parser.add_argument(
        "--targets",
        default=",".join(DEFAULT_TARGETS),
        help="Comma-separated fallback targets for every generated entry.",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "index.json"),
        help="Output index.json path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    targets = normalize_targets(args.targets)
    collection_roots = normalize_collection_roots(args.collection_roots)
    if not targets:
        print("error: at least one target is required", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="aism-registry-") as checkout_root:
        repo_dir = Path(checkout_root) / "source"
        clone_repo(args.repo_url, args.ref, repo_dir)

        entries, stats = collect_entries(
            repo_dir=repo_dir,
            collection_roots=collection_roots,
            repo_url=args.repo_url,
            ref=args.ref,
            registry_source=args.source,
            targets=targets,
        )

    payload = {
        "skills": [entry.to_index_record() for entry in sorted(entries.values(), key=lambda item: item.slug)]
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    print(
        "generated {count} skills to {output} (root_scanned={root_scanned}, collections_scanned={collection_scanned}, hidden={hidden}, missing_skill_md={missing}, deduped={deduped})".format(
            count=len(payload["skills"]),
            output=output_path,
            root_scanned=stats["root_scanned"],
            collection_scanned=stats["collection_scanned"],
            hidden=stats["hidden"],
            missing=stats["missing_skill_md"],
            deduped=stats["deduped"],
        )
    )
    return 0


def clone_repo(repo_url: str, ref: str, destination: Path) -> None:
    cmd = ["git", "clone", "--depth", "1", "--branch", ref, repo_url, str(destination)]
    subprocess.run(cmd, check=True)


def collect_entries(
    repo_dir: Path,
    collection_roots: list[str],
    repo_url: str,
    ref: str,
    registry_source: str,
    targets: list[str],
) -> tuple[dict[str, SkillEntry], dict[str, int]]:
    entries: dict[str, SkillEntry] = {}
    stats = {
        "root_scanned": 0,
        "collection_scanned": 0,
        "hidden": 0,
        "missing_skill_md": 0,
        "deduped": 0,
    }

    excluded_root_dirs = {Path(root).parts[0] for root in collection_roots if root}

    for skill_dir in sorted(repo_dir.iterdir(), key=lambda item: item.name.lower()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name in excluded_root_dirs:
            continue

        stats["root_scanned"] += 1
        ingest_directory(
            entries=entries,
            stats=stats,
            skill_dir=skill_dir,
            relative_path=skill_dir.name,
            repo_url=repo_url,
            ref=ref,
            registry_source=registry_source,
            targets=targets,
        )

    for collection_root in collection_roots:
        source_root = repo_dir / collection_root
        if not source_root.is_dir():
            print(
                f"warning: collection root {collection_root!r} not found in cloned repository",
                file=sys.stderr,
            )
            continue

        for skill_dir in sorted(source_root.iterdir(), key=lambda item: item.name.lower()):
            if not skill_dir.is_dir():
                continue

            stats["collection_scanned"] += 1
            ingest_directory(
                entries=entries,
                stats=stats,
                skill_dir=skill_dir,
                relative_path=f"{collection_root.strip('/')}/{skill_dir.name}",
                repo_url=repo_url,
                ref=ref,
                registry_source=registry_source,
                targets=targets,
            )

    return entries, stats


def ingest_directory(
    entries: dict[str, SkillEntry],
    stats: dict[str, int],
    skill_dir: Path,
    relative_path: str,
    repo_url: str,
    ref: str,
    registry_source: str,
    targets: list[str],
) -> None:
    if skill_dir.name.startswith("."):
        stats["hidden"] += 1
        return

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        stats["missing_skill_md"] += 1
        return

    entry = build_entry(
        skill_dir=skill_dir,
        relative_path=relative_path,
        repo_url=repo_url,
        ref=ref,
        registry_source=registry_source,
        targets=targets,
    )
    if entry is None:
        return

    current = entries.get(entry.slug)
    if current is None:
        entries[entry.slug] = entry
        return

    preferred = prefer_entry(current, entry)
    if preferred is not current:
        entries[entry.slug] = preferred
    stats["deduped"] += 1


def build_entry(
    skill_dir: Path,
    relative_path: str,
    repo_url: str,
    ref: str,
    registry_source: str,
    targets: list[str],
) -> SkillEntry | None:
    metadata = parse_frontmatter(skill_dir / "SKILL.md")
    slug = normalize_slug(skill_dir.name)
    if not slug:
        return None

    name = clean_scalar(metadata.get("name")) or skill_dir.name
    if normalize_slug(name) == slug:
        name = slug
    description = clean_scalar(metadata.get("description"))
    version = clean_scalar(metadata.get("version")) or DEFAULT_VERSION

    return SkillEntry(
        slug=slug,
        name=name,
        description=description,
        version=version,
        repo=repo_url,
        path=relative_path.strip("/"),
        ref=ref,
        source=registry_source.strip().lower(),
        targets=targets,
        folder=skill_dir.name,
    )


def parse_frontmatter(skill_md: Path) -> dict[str, str]:
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = clean_scalar(value)

    return metadata


def clean_scalar(value: str | None) -> str:
    if value is None:
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text.strip()


def normalize_slug(value: str) -> str:
    text = value.strip().lower().replace("_", "-").replace(" ", "-")
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def normalize_targets(raw: str) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for target in raw.split(","):
        target = target.strip().lower()
        if not target or target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets


def normalize_collection_roots(raw_roots: list[str] | None) -> list[str]:
    roots = raw_roots or DEFAULT_COLLECTION_ROOTS
    normalized: list[str] = []
    seen: set[str] = set()
    for root in roots:
        value = root.strip().strip("/")
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def prefer_entry(current: SkillEntry, candidate: SkillEntry) -> SkillEntry:
    return min((current, candidate), key=entry_preference_key)


def entry_preference_key(entry: SkillEntry) -> tuple[bool, bool, str]:
    return ("_" in entry.folder, entry.folder.startswith("-"), entry.folder)


if __name__ == "__main__":
    raise SystemExit(main())
