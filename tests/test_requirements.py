from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def test_requirement_pins_keep_upper_bounds_and_match_this_environment():
    lines = [line.strip() for line in Path("requirements.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    assert lines
    for line in lines:
        requirement = Requirement(line)
        assert requirement.specifier, line
        assert any(spec.operator in {"<", "<="} for spec in requirement.specifier), line
        installed = Version(version(requirement.name))
        assert installed in requirement.specifier, f"{requirement.name} {installed} is outside {requirement.specifier}"
