#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _archive_name(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path("external") / path.name


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Package generated DMR analysis products without raw IQ files."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    root = Path.cwd().resolve()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    missing: list[Path] = []
    for raw in args.inputs:
        path = raw.expanduser().resolve()
        if not path.exists():
            missing.append(raw)
            continue
        if path.is_file():
            files.append(path)
        else:
            files.extend(item for item in sorted(path.rglob("*")) if item.is_file())

    if missing:
        missing_text = ", ".join(str(path) for path in missing)
        raise SystemExit(f"Missing input paths: {missing_text}")
    if not files:
        raise SystemExit("No files found to package")

    unique_files = list(dict.fromkeys(files))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in unique_files:
            archive.write(path, _archive_name(path, root))

    digest = _sha256(output)
    print(f"Archive: {output}")
    print(f"Files:   {len(unique_files):,}")
    print(f"Bytes:   {output.stat().st_size:,}")
    print(f"SHA256:  {digest}")


if __name__ == "__main__":
    main()
