from pathlib import Path
from shutil import copytree, rmtree

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    """Stage the canonical root specification as package data during builds."""

    def run(self) -> None:
        root = Path(__file__).parent
        source = root / "spec"
        target = root / "src" / "modfig" / "resources" / "spec"
        staged = not target.exists()
        if staged:
            copytree(source, target)
        try:
            super().run()
        finally:
            if staged:
                rmtree(target)


setup(cmdclass={"build_py": build_py})
