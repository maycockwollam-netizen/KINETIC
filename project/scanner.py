"""Project scanning.

Detects project type, languages, package managers, manifests, lockfiles, build
and test systems, and Git/Docker presence — purely from filesystem inspection.
It never reads file *contents* into model context; only names/paths/types.

Detection is conservative: we only report something when the corresponding
manifest file exists. No guessing.
"""

from __future__ import annotations

from pathlib import Path

from project.models import ManifestFile, ProjectManifest

# --- detection tables --------------------------------------------------------

#: (filename, kind, language, package_manager, lockfile_for)
_PYTHON_MANIFESTS = {
    "pyproject.toml": ("python:pyproject", "python", None),
    "setup.py": ("python:setup-py", "python", None),
    "setup.cfg": ("python:setup-cfg", "python", None),
    "requirements.txt": ("python:requirements", "python", "pip"),
    "requirements-dev.txt": ("python:requirements-dev", "python", "pip"),
    "uv.lock": ("python:uv-lock", "python", "uv"),
    "poetry.lock": ("python:poetry-lock", "python", "poetry"),
    "Pipfile.lock": ("python:pipenv-lock", "python", "pipenv"),
}

_NODE_MANIFESTS = {
    "package.json": ("node:package-json", "node", None),
    "package-lock.json": ("node:package-lock", "node", "npm"),
    "pnpm-lock.yaml": ("node:pnpm-lock", "node", "pnpm"),
    "yarn.lock": ("node:yarn-lock", "node", "yarn"),
    "bun.lockb": ("node:bun-lock", "node", "bun"),
}

_RUST_MANIFESTS = {
    "Cargo.toml": ("rust:cargo-toml", "rust", None),
    "Cargo.lock": ("rust:cargo-lock", "rust", "cargo"),
}

_GOLANG_MANIFESTS = {
    "go.mod": ("go:go-mod", "go", "go"),
    "go.sum": ("go:go-sum", "go", "go"),
}

_BUILD_FILES = {
    "Makefile": "make",
    "makefile": "make",
    "CMakeLists.txt": "cmake",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pom.xml": "maven",
    "Justfile": "just",
    "justfile": "just",
}

_TEST_CONFIG = {
    "pytest.ini": "pytest",
    "tox.ini": "tox",
    "jest.config.js": "jest",
    "jest.config.ts": "jest",
    "vitest.config.ts": "vitest",
    "vitest.config.js": "vitest",
    ".mocharc.yml": "mocha",
    "phpunit.xml": "phpunit",
}

_DOCKER_FILES = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


def find_project_root(start: Path) -> Path:
    """Walk upward from ``start`` to the nearest directory containing ``.git``.

    Falls back to ``start`` itself if no Git root is found.
    """
    start = start.resolve()
    if not start.is_dir():
        start = start.parent
    current = start
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start


def scan_project(root: Path) -> ProjectManifest:
    """Scan ``root`` and return structured project metadata."""
    root = root.resolve()
    manifest = ProjectManifest(root=root)
    manifest.git_repository = (root / ".git").exists()

    for name, (kind, lang, pm) in _PYTHON_MANIFESTS.items():
        _check(root, name, kind, lang, pm, manifest)
    for name, (kind, lang, pm) in _NODE_MANIFESTS.items():
        _check(root, name, kind, lang, pm, manifest)
    for name, (kind, lang, pm) in _RUST_MANIFESTS.items():
        _check(root, name, kind, lang, pm, manifest)
    for name, (kind, lang, pm) in _GOLANG_MANIFESTS.items():
        _check(root, name, kind, lang, pm, manifest)

    # Build systems.
    for fname, system in _BUILD_FILES.items():
        if (root / fname).exists():
            if system not in manifest.build_systems:
                manifest.build_systems.append(system)
            manifest.manifests.append(ManifestFile(path=fname, kind=f"build:{system}"))

    # Test configs.
    for fname, system in _TEST_CONFIG.items():
        if (root / fname).exists():
            if system not in manifest.test_systems:
                manifest.test_systems.append(system)
            manifest.manifests.append(ManifestFile(path=fname, kind=f"test:{system}"))

    # Docker.
    for fname in _DOCKER_FILES:
        if (root / fname).exists():
            manifest.docker = True
            kind = "docker:compose" if "compose" in fname else "docker:dockerfile"
            manifest.manifests.append(ManifestFile(path=fname, kind=kind))

    # Framework heuristics based on package.json presence + common deps dir.
    _detect_frameworks(root, manifest)

    # Deduplicate while preserving order.
    manifest.languages = _dedupe(manifest.languages)
    manifest.package_managers = _dedupe(manifest.package_managers)
    return manifest


def _check(
    root: Path,
    filename: str,
    kind: str,
    language: str | None,
    package_manager: str | None,
    manifest: ProjectManifest,
) -> None:
    path = root / filename
    if not path.exists():
        return
    manifest.manifests.append(ManifestFile(path=filename, kind=kind))
    if language and language not in manifest.languages:
        manifest.languages.append(language)
    if package_manager and package_manager not in manifest.package_managers:
        manifest.package_managers.append(package_manager)


def _detect_frameworks(root: Path, manifest: ProjectManifest) -> None:
    """Infer frameworks from known marker files only (no content parsing)."""
    markers = {
        "next.config.js": "next.js",
        "next.config.ts": "next.js",
        "next.config.mjs": "next.js",
        "nuxt.config.ts": "nuxt",
        "nuxt.config.js": "nuxt",
        "angular.json": "angular",
        "vue.config.js": "vue",
        "svelte.config.js": "svelte",
        "manage.py": "django",
        "wsgi.py": "django",
        "asgi.py": "django",
        "app.py": "flask",
        # Flask/FastAPI are hard to distinguish by file alone; app.py is a weak signal only.
    }
    for fname, framework in markers.items():
        if (root / fname).exists() and framework not in manifest.frameworks:
            manifest.frameworks.append(framework)
            manifest.manifests.append(ManifestFile(path=fname, kind=f"framework:{framework}"))


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
