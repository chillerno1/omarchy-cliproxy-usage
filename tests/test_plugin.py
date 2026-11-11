"""Static checks on the plugin files: manifest rules the Omarchy registry
enforces, and the plain-text rule for every QML Text that shows data.

Run: python3 -m unittest discover -s tests
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Manifest(unittest.TestCase):
  def setUp(self):
    self.manifest = json.loads((ROOT / "manifest.json").read_text())

  def test_registry_rules(self):
    m = self.manifest
    self.assertIs(m["schemaVersion"], 1)
    for field in ("id", "name", "version", "author", "description", "kinds", "entryPoints"):
      self.assertIn(field, m)
    self.assertRegex(m["id"], r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    self.assertFalse(m["id"].startswith("omarchy."))
    self.assertNotIn("clonedFrom", json.dumps(m))
    self.assertIn(m["barWidget"]["defaultSection"], ["left", "centerBegin", "center", "centerEnd", "tray", "right"])
    for entry in m["entryPoints"].values():
      self.assertFalse(entry.startswith("/") or ".." in entry)
      self.assertTrue((ROOT / entry).is_file(), entry)

  def test_qml_names_the_manifest_id(self):
    panel = (ROOT / "Panel.qml").read_text()
    self.assertIn(f'moduleName: "{self.manifest["id"]}"', panel)
    self.assertIn(f'ipcTarget: "{self.manifest["id"]}"', panel)

  def test_bar_slot_is_sized_by_the_icon(self):
    panel = (ROOT / "Panel.qml").read_text()
    self.assertIn("implicitWidth: button.implicitWidth", panel)
    self.assertIn("implicitHeight: button.implicitHeight", panel)

  def test_version_matches_changelog(self):
    changelog = (ROOT / "CHANGELOG.md").read_text()
    self.assertIn(f"## {self.manifest['version']}", changelog)

  def test_no_symlinks(self):
    links = [p for p in ROOT.rglob("*") if p.is_symlink() and ".git" not in p.parts]
    self.assertEqual(links, [])


class PlainText(unittest.TestCase):
  def test_every_text_is_plain(self):
    """QML Text defaults to AutoText, which renders markup-shaped data (an
    email, a provider's error) as rich text and can load remote images."""
    panel = (ROOT / "Panel.qml").read_text()
    blocks = [m.start() for m in re.finditer(r"\bText \{", panel)]
    self.assertGreater(len(blocks), 0)
    for start in blocks:
      depth, end = 0, start
      for end in range(panel.index("{", start), len(panel)):
        depth += {"{": 1, "}": -1}.get(panel[end], 0)
        if depth == 0:
          break
      body = panel[start:end]
      line = panel.count("\n", 0, start) + 1
      self.assertIn("textFormat: Text.PlainText", body, f"Panel.qml:{line} Text without PlainText")

  def test_strings_are_complete(self):
    used = set(re.findall(r'strings\.tn?\("([^"]+)"', (ROOT / "Panel.qml").read_text()))
    english = json.loads((ROOT / "i18n" / "en.json").read_text())
    self.assertEqual(sorted(used - set(english)), [])


if __name__ == "__main__":
  unittest.main()
