# aism-registry

Curated registry data for [AI Skills Manager](https://github.com/AcaciusShun/AI-Skills-Manager).

This repository publishes a plain `index.json` that `aism` can consume through a
local file path or a hosted raw GitHub URL.

## Repository Layout

```text
index.json
sources.json
scripts/
  generate_index.py
  generate_composio_index.py
```

## Registry Sources

The registry is generated from `sources.json`.

Current configured source:

- `composiohq`
  - repo: `https://github.com/ComposioHQ/awesome-claude-skills.git`
  - ref: `master`
  - scans root-level skill directories
  - scans `composio-skills/` as a bundled collection root

The generic generator can merge multiple upstream repositories into one unified
`index.json`. Each source defines:

- `id`
- `repo`
- `ref`
- `scan_root`
- `collection_roots`
- `targets`

## Curation Rules

- one registry entry per skill directory
- root-level skill folders are indexed with paths like `theme-factory`
- bundled skills are indexed with paths like `composio-skills/ably-automation`
- `slug` is normalized from the directory name
- `name` is the display name from `SKILL.md` frontmatter when present
- `version` falls back to `0.1.0` when the source skill omits it
- default `targets` for this source are `["claude"]`
- hidden directories are skipped
- normalized slug collisions prefer the already hyphenated folder over an
  underscore variant
- slug collisions across different sources fail the build instead of silently
  overriding an existing entry

This keeps the registry stable even when upstream monorepos contain mixed naming
styles such as `google-admin-automation` and `google_admin-automation`.

## Registry Format

`aism` expects one entry per installable skill:

```json
{
  "skills": [
    {
      "slug": "algorithmic-art",
      "name": "algorithmic-art",
      "repo": "https://github.com/ComposioHQ/awesome-claude-skills.git",
      "path": "composio-skills/algorithmic-art",
      "ref": "master",
      "source": "composiohq",
      "version": "0.1.0",
      "description": "Create algorithmic art",
      "targets": ["claude"]
    }
  ]
}
```

## Regenerate `index.json`

From this repository root:

```bash
python3 scripts/generate_index.py
```

Useful overrides:

```bash
python3 scripts/generate_index.py --output /tmp/index.json
python3 scripts/generate_index.py --config ./sources.json
python3 scripts/generate_index.py --source composiohq
```

The script performs a temporary shallow clone of the upstream repository and
rewrites `index.json` deterministically.

`scripts/generate_composio_index.py` is kept as a compatibility wrapper for the
current Composio source.

## Add Another Upstream Source

Append another source object to `sources.json`, for example:

```json
{
  "id": "anthropics",
  "repo": "https://github.com/anthropics/skills.git",
  "ref": "main",
  "scan_root": false,
  "collection_roots": ["skills"],
  "targets": ["claude", "codex", "gemini"]
}
```

Then regenerate:

```bash
python3 scripts/generate_index.py
```

## Automatic Refresh

This repository includes a scheduled GitHub Actions workflow that regenerates
`index.json` and commits it back to `main` when upstream content changes.

Current triggers:

- daily scheduled refresh
- manual `workflow_dispatch`
- pushes that change the generator or workflow itself

## Consume From `aism`

Local development:

```json
{
  "registry_profile": "default",
  "registry_profiles": {
    "default": "file:///Users/nagihsu/gitHub/aism-registry/index.json"
  }
}
```

After publishing this repository, use the raw GitHub URL:

```text
https://raw.githubusercontent.com/<owner>/aism-registry/main/index.json
```
