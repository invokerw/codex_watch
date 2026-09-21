"""Keep release automation aligned with the package's publishing contract."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_tests_and_builds(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("python-version: '3.12'", workflow)
        self.assertIn("python -m build --outdir dist", workflow)
        self.assertIn("python -m unittest discover -s tests -p 'test_*.py' -v", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)

    def test_release_is_tagged_tested_and_trusted(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("tags:\n      - 'v*'", workflow)
        self.assertIn("Verify tag matches package version", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("name: pypi", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow)
        self.assertIn("python -m unittest discover -s tests -p 'test_*.py' -v", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("GH_REPO: ${{ github.repository }}", workflow)
        self.assertNotIn("PYPI_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
