"""版本号一致性：源码、发行元数据、已安装包必须同源。

背景：v1.1.1 发布时包内 `__version__` 仍是 1.1.0，宿主用它打点会误报版本。
现在版本只在 `enter_harness/_version.py` 定义一次，其余地方读取。
"""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def read_toml():
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


class VersionTests(unittest.TestCase):
    def test_package_version_comes_from_single_module(self):
        import enter_harness
        from enter_harness import _version

        self.assertEqual(enter_harness.__version__, _version.__version__)
        self.assertRegex(enter_harness.__version__, r"^\d+\.\d+\.\d+([.-].+)?$")

    def test_pyproject_has_no_hardcoded_version(self):
        data = read_toml()
        project = data["project"]
        self.assertNotIn("version", project, "pyproject 里又写死版本了")
        self.assertIn("version", project.get("dynamic", []))

    def test_pyproject_dynamic_version_points_at_version_module(self):
        data = read_toml()
        attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "enter_harness._version.__version__")

    def test_init_does_not_redefine_version_literal(self):
        source = (ROOT / "enter_harness" / "__init__.py").read_text(encoding="utf-8")
        literals = re.findall(r'__version__\s*=\s*["\']', source)
        self.assertEqual(literals, [], "enter_harness/__init__.py 不要再写死 __version__")

    def test_installed_metadata_matches_when_package_installed(self):
        from importlib import metadata

        from enter_harness import __version__

        try:
            installed = metadata.version("enter-harness")
        except metadata.PackageNotFoundError:
            self.skipTest("enter-harness 未以发行包安装（源码树直接运行）")
        self.assertEqual(installed, __version__)


if __name__ == "__main__":
    unittest.main()
