from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_EXTENSIONS = frozenset({".py", ".js", ".css", ".html"})
DEFAULT_EXCLUDED_DIRS = frozenset(
    {".git", ".venv", ".tmp", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


class FileLineCount(BaseModel):
    path: str
    lines: int = Field(ge=0)


class PackageLineCount(BaseModel):
    package: str
    files: int = Field(ge=0)
    lines: int = Field(ge=0)
    test_lines: int = Field(ge=0)


class ScaleReport(BaseModel):
    root: str
    total_files: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    test_files: int = Field(ge=0)
    test_lines: int = Field(ge=0)
    packages: list[PackageLineCount] = Field(default_factory=list)
    target_lines: int = Field(default=100_000, ge=1)

    @property
    def progress(self) -> float:
        return min(1.0, self.total_lines / self.target_lines)

    @property
    def remaining_lines(self) -> int:
        return max(0, self.target_lines - self.total_lines)


def analyze_repository(
    root: str | Path,
    *,
    target_lines: int = 100_000,
    extensions: set[str] | frozenset[str] = DEFAULT_EXTENSIONS,
    excluded_dirs: set[str] | frozenset[str] = DEFAULT_EXCLUDED_DIRS,
) -> ScaleReport:
    root_path = Path(root).resolve()
    files = [
        path
        for path in root_path.rglob("*")
        if path.is_file()
        and path.suffix.lower() in extensions
        and not any(part in excluded_dirs for part in path.parts)
    ]
    package_data: dict[str, list[FileLineCount]] = {}
    for path in files:
        relative = path.relative_to(root_path)
        package = _package_name(relative)
        package_data.setdefault(package, []).append(
            FileLineCount(path=relative.as_posix(), lines=_count_lines(path))
        )
    package_counts = [
        PackageLineCount(
            package=package,
            files=len(entries),
            lines=sum(entry.lines for entry in entries),
            test_lines=sum(entry.lines for entry in entries if _is_test(entry.path)),
        )
        for package, entries in sorted(package_data.items())
    ]
    return ScaleReport(
        root=str(root_path),
        total_files=len(files),
        total_lines=sum(entry.lines for entries in package_data.values() for entry in entries),
        test_files=sum(1 for path in files if _is_test(path.relative_to(root_path).as_posix())),
        test_lines=sum(
            entry.lines
            for entries in package_data.values()
            for entry in entries
            if _is_test(entry.path)
        ),
        packages=package_counts,
        target_lines=target_lines,
    )


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except UnicodeDecodeError:
        return 0


def _is_test(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or Path(path).name.startswith("test_")
    )


def _package_name(path: Path) -> str:
    parts = path.parts
    if parts and parts[0] in {"src", "tests", "examples"}:
        return parts[0] if len(parts) == 1 else "/".join(parts[:2])
    return parts[0] if parts else "."
