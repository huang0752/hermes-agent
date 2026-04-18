"""Regression tests for packaging metadata in pyproject.toml."""

from pathlib import Path
import tomllib


def _load_optional_dependencies():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["optional-dependencies"]


def test_matrix_extra_linux_only_in_all():
    """mautrix[encryption] depends on python-olm which is upstream-broken on
    modern macOS (archived libolm, C++ errors with Clang 21+).  The [matrix]
    extra is included in [all] but gated to Linux via a platform marker so
    that ``hermes update`` doesn't fail on macOS."""
    optional_dependencies = _load_optional_dependencies()

    assert "matrix" in optional_dependencies
    # Must NOT be unconditional — python-olm has no macOS wheels.
    assert "hermes-agent[matrix]" not in optional_dependencies["all"]
    # Must be present with a Linux platform marker.
    linux_gated = [
        dep for dep in optional_dependencies["all"]
        if "matrix" in dep and "linux" in dep
    ]
    assert linux_gated, "expected hermes-agent[matrix] with sys_platform=='linux' marker in [all]"


def test_juhe_extra_is_explicit_and_not_in_all():
    """Private Juhe support should stay opt-in so upstream installs do not
    break on a private dependency."""
    optional_dependencies = _load_optional_dependencies()

    assert "juhe" in optional_dependencies
    juhe_extra = optional_dependencies["juhe"]

    assert any(dep.startswith("aiohttp>=") for dep in juhe_extra)
    assert any(dep.startswith("boto3>=") for dep in juhe_extra)
    assert any(
        dep == "qwsaas @ git+ssh://git@github.com:22/huang0752/qwsaas.git@v0.1.0"
        for dep in juhe_extra
    )
    assert "hermes-agent[juhe]" not in optional_dependencies["all"]
