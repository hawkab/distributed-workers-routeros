import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

class PipelineEntrypoints(unittest.TestCase):
    def test_all_forges_call_existing_release_scripts(self):
        for relative in ("gitflic-ci.yaml", ".github/workflows/release.yml", ".gitea/workflows/release.yml"):
            with self.subTest(pipeline=relative):
                commands = re.findall(r"python3 -B (ci/[A-Za-z0-9_./-]+\.py)", (ROOT / relative).read_text())
                self.assertTrue(commands)
                for command in commands:
                    self.assertTrue((ROOT / command).is_file(), command)
