#!/usr/bin/env python3
"""Generate a merged aism-compatible registry index from configured upstream sources."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "sources.json"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parents[1] / "index.json"
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


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    repo: str
    ref: str
    scan_root: bool
    collection_roots: list[str]
    targets: list[str]

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "SourceConfig":
        source_id = normalize_source_id(str(raw.get("id", "")))
        if not source_id:
            raise ValueError("source entry is missing a valid id")

        repo = str(raw.get("repo", "")).strip()
        if not repo:
            raise ValueError(f"source {source_id!r} is missing repo")

        ref = str(raw.get("ref", "")).strip()
        if not ref:
            raise ValueError(f"source {source_id!r} is missing ref")

        raw_scan_root = raw.get("scan_root", False)
        scan_root = bool(raw_scan_root)

        raw_collection_roots = raw.get("collection_roots", [])
        if raw_collection_roots is None:
            raw_collection_roots = []
        if not isinstance(raw_collection_roots, list):
            raise ValueError(f"source {source_id!r} collection_roots must be a list")
        collection_roots = normalize_collection_roots([str(item) for item in raw_collection_roots])

        raw_targets = raw.get("targets", [])
        if not isinstance(raw_targets, list):
            raise ValueError(f"source {source_id!r} targets must be a list")
        targets = normalize_targets([str(item) for item in raw_targets])
        if not targets:
            raise ValueError(f"source {source_id!r} must define at least one target")

        return cls(
            source_id=source_id,
            repo=repo,
            ref=ref,
            scan_root=scan_root,
            collection_roots=collection_roots,
            targets=targets,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a merged index.json from configured upstream skill sources."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the registry sources configuration file.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output index.json path.",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="selected_sources",
        default=None,
        help="Only generate entries for the selected source id. Repeatable.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    try:
        sources = load_sources(config_path)
        sources = filter_sources(sources, args.selected_sources)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if not sources:
        print("error: no sources selected", file=sys.stderr)
        return 1

    merged_entries: dict[str, SkillEntry] = {}
    source_summaries: list[tuple[SourceConfig, dict[str, int], int]] = []

    try:
        with tempfile.TemporaryDirectory(prefix="aism-registry-") as checkout_root:
            checkout_root_path = Path(checkout_root)
            for source in sources:
                repo_dir = checkout_root_path / source.source_id
                clone_repo(source.repo, source.ref, repo_dir)

                source_entries, stats = collect_source_entries(repo_dir, source)
                merge_source_entries(merged_entries, source_entries)
                source_summaries.append((source, stats, len(source_entries)))
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    payload = {
        "skills": [
            entry.to_index_record()
            for entry in sorted(merged_entries.values(), key=lambda item: item.slug)
        ]
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    for source, stats, entry_count in source_summaries:
        print(
            "[{source}] entries={entries} root_scanned={root_scanned} collections_scanned={collection_scanned} hidden={hidden} missing_skill_md={missing} deduped={deduped}".format(
                source=source.source_id,
                entries=entry_count,
                root_scanned=stats["root_scanned"],
                collection_scanned=stats["collection_scanned"],
                hidden=stats["hidden"],
                missing=stats["missing_skill_md"],
                deduped=stats["deduped"],
            )
        )

    print(f"generated {len(payload['skills'])} skills to {output_path}")
    return 0


def load_sources(config_path: Path) -> list[SourceConfig]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ValueError(f"config file not found: {config_path}") from err
    except json.JSONDecodeError as err:
        raise ValueError(f"decode config file {config_path}: {err}") from err

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError('config file must contain a top-level "sources" array')

    sources: list[SourceConfig] = []
    seen_ids: set[str] = set()
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise ValueError("each source entry must be an object")
        source = SourceConfig.from_dict(raw_source)
        if source.source_id in seen_ids:
            raise ValueError(f"duplicate source id {source.source_id!r} in config")
        seen_ids.add(source.source_id)
        sources.append(source)

    return sources


def filter_sources(sources: list[SourceConfig], selected_sources: list[str] | None) -> list[SourceConfig]:
    if not selected_sources:
        return sources

    requested = [normalized for item in selected_sources if (normalized := normalize_source_id(item))]
    selected = [source for source in sources if source.source_id in requested]
    missing = sorted(set(requested) - {source.source_id for source in selected})
    if missing:
        raise ValueError(f"unknown source id(s): {', '.join(missing)}")
    return selected


def clone_repo(repo_url: str, ref: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_git(["clone", "--depth", "1", "--branch", ref, repo_url, str(destination)])
        return
    except RuntimeError:
        pass

    run_git(["clone", "--depth", "1", repo_url, str(destination)])
    run_git(["fetch", "--depth", "1", "origin", ref], cwd=destination)
    run_git(["checkout", "--detach", "FETCH_HEAD"], cwd=destination)


def run_git(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return

    details = result.stderr.strip() or result.stdout.strip() or "git command failed"
    raise RuntimeError(f"git {' '.join(args)}: {details}")


def collect_source_entries(
    repo_dir: Path,
    source: SourceConfig,
) -> tuple[dict[str, SkillEntry], dict[str, int]]:
    entries: dict[str, SkillEntry] = {}
    stats = {
        "root_scanned": 0,
        "collection_scanned": 0,
        "hidden": 0,
        "missing_skill_md": 0,
        "deduped": 0,
    }

    excluded_root_dirs = {Path(root).parts[0] for root in source.collection_roots if root}

    if source.scan_root:
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
                source=source,
            )

    for collection_root in source.collection_roots:
        source_root = repo_dir / collection_root
        if not source_root.is_dir():
            print(
                f"warning: source {source.source_id!r} collection root {collection_root!r} not found",
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
                source=source,
            )

    return entries, stats


def ingest_directory(
    entries: dict[str, SkillEntry],
    stats: dict[str, int],
    skill_dir: Path,
    relative_path: str,
    source: SourceConfig,
) -> None:
    if skill_dir.name.startswith("."):
        stats["hidden"] += 1
        return

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        stats["missing_skill_md"] += 1
        return

    entry = build_entry(skill_dir=skill_dir, relative_path=relative_path, source=source)
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
    source: SourceConfig,
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
        repo=source.repo,
        path=relative_path.strip("/"),
        ref=source.ref,
        source=source.source_id,
        targets=source.targets,
        folder=skill_dir.name,
    )


def merge_source_entries(
    merged_entries: dict[str, SkillEntry],
    source_entries: dict[str, SkillEntry],
) -> None:
    for slug, entry in source_entries.items():
        existing = merged_entries.get(slug)
        if existing is None:
            merged_entries[slug] = entry
            continue

        raise ValueError(
            'duplicate slug "{slug}" across sources: {left_source}:{left_path} and {right_source}:{right_path}'.format(
                slug=slug,
                left_source=existing.source,
                left_path=existing.path,
                right_source=entry.source,
                right_path=entry.path,
            )
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


def normalize_source_id(value: str) -> str:
    return normalize_slug(value)


def normalize_targets(raw_targets: list[str]) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for target in raw_targets:
        normalized = target.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        targets.append(normalized)
    return targets


def normalize_collection_roots(raw_roots: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for root in raw_roots:
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
