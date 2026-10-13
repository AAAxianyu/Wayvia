"""Exercise the real source builder against an unrelated external symlink fixture."""

import tarfile
from pathlib import Path

from hatchling.builders.sdist import SdistBuilder


def test_sdist_only_traverses_explicit_project_roots(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_bytes((repository / "pyproject.toml").read_bytes())
    (project / "src/wayvia").mkdir(parents=True)
    (project / "src/wayvia/__init__.py").write_text('__version__ = "0.1.0"\n')
    (project / "docs/licenses").mkdir(parents=True)
    (project / "docs/licenses/source.txt").write_text("fixture third-party notice")
    (project / "README.md").write_text("Wayvia fixture")
    (project / "uv.lock").write_text("version = 1\n")
    unrelated = project / "unrelated-project"
    unrelated.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unrelated private material")
    (unrelated / "external-link").symlink_to(outside)
    (unrelated / "private.txt").write_text("unrelated private material")
    (project / "unrelated-root.txt").write_text("not a Wayvia source input")
    distributions = tmp_path / "dist"
    distributions.mkdir()
    builder = SdistBuilder(str(project))
    archive_path = next(builder.build(directory=str(distributions), versions=["standard"]))
    with tarfile.open(archive_path) as archive:
        names = {"/".join(Path(member.name).parts[1:]) for member in archive.getmembers()}
    assert "src/wayvia/__init__.py" in names
    assert "docs/licenses/source.txt" in names
    assert "uv.lock" in names
    assert "README.md" in names
    assert not any("unrelated" in name or "external-link" in name for name in names)
    assert outside.read_text() == "unrelated private material"
