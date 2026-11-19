#!/usr/bin/env python3
"""Render the real panel content offscreen with fictional accounts.

Requires an installed Omarchy shell and Quickshell. Never runs the collector
or changes the installed plugin. Run from anywhere: python3 scripts/render-preview.py
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SHELL = Path("/usr/share/omarchy/shell")
NOW = 1805673600000


def account(provider, name, email, plan, session, weekly, extra=""):
  return dict(provider=provider, providerName=name, email=email, plan=plan,
              status="ok", disabled=False, note="", extra=extra, fetchedAt=NOW,
              limits=[dict(title="Session", percent=session, resetsAt=NOW + 9300000),
                      dict(title="Weekly", percent=weekly, resetsAt=NOW + 216000000)])


record = dict(
  proxy=dict(up=True, port=8317),
  gatewayPolicy=dict(strategy="round-robin", sessionAffinity=False,
                     retryRounds=3, credentialsPerRound=0),
  accounts=[
    account("claude", "Claude", "sam@example.com", "Max 20x", .42, .61),
    account("claude", "Claude", "sam.work@example.org", "Max 5x", .08, .18),
    account("codex", "Codex", "sam@example.com", "Pro", .12, .35, "Credits 1,250"),
    account("antigravity", "Antigravity", "sam@example.com", "Google AI Pro", .27, .22),
    account("kimi", "Kimi", "", "", .55, .30),
  ],
)
record["accounts"][0]["limits"].append(dict(title="Fable weekly", percent=.93, resetsAt=NOW + 216000000))


def main():
  source = (ROOT / "Panel.qml").read_text()
  # Keep the actual properties, helpers, column and inline components. Omit
  # only the collector, IPC, bar window and popup lifecycle for this render.
  prefix = source[:source.index("  // ------------------------------------------------------------- data")]
  prefix = prefix.replace("Panel {", "Item {", 1)
  prefix = prefix.replace('  moduleName: "chillerno1.cliproxy-usage"', "  property var bar: null")
  prefix = prefix.replace('  ipcTarget: "chillerno1.cliproxy-usage"', "")
  prefix = prefix.replace("  manageIpc: false", "")
  prefix = prefix.replace("  implicitWidth: button.implicitWidth", "  width: 408")
  prefix = prefix.replace("  implicitHeight: button.implicitHeight", "  height: column.implicitHeight + 28")
  prefix = prefix.replace("  property var record: null", "  property var record: " + json.dumps(record))
  prefix = prefix.replace("  property double nowMs: Date.now()", f"  property double nowMs: {NOW}")
  prefix += '''
  function setting(key, fallback) { return fallback }
  QtObject { id: button; property string text: "󰒍" }
  QtObject { id: updateProcess; property bool running: false }
  Rectangle { anchors.fill: parent; color: root.surface }
'''
  start = source.index("        Column {\n          id: column")
  end = source.index("\n      }\n    }\n  }\n\n  // ------------------------------------------------------------- components", start)
  column = source[start:end]
  lines = column.splitlines()
  lines = [line for line in lines if not line.strip().startswith(("x: LayoutMirroring", "width: panelFlick.width"))]
  lines.insert(2, "          x: 14; y: 14; width: parent.width - 28")
  components = source[source.index("  // ------------------------------------------------------------- components"):]
  scene = prefix + "\n".join(lines) + "\n" + components

  with tempfile.TemporaryDirectory(prefix="cliproxy-preview-") as tmp:
    work = Path(tmp)
    for name in ("Commons", "Ui"):
      shutil.copytree(SHELL / name, work / name)
    for name in ("assets", "i18n"):
      shutil.copytree(ROOT / name, work / name)
    (work / "Preview.qml").write_text(scene)
    (work / "shell.qml").write_text('''
import QtQuick
import QtQuick.Window
import Quickshell
Window {
  visible: true
  width: scene.width
  height: scene.height
  Preview { id: scene }
  Timer {
    interval: 1200; running: true
    onTriggered: scene.grabToImage(function(result) {
      if (!result.saveToFile(Quickshell.env("CLIPROXY_PREVIEW_OUTPUT")))
        console.error("Failed to save preview")
      Qt.quit()
    })
  }
}
''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               CLIPROXY_PREVIEW_OUTPUT=str(ROOT / "preview.png"))
    subprocess.run(["quickshell", "--no-color", "-p", str(work)], env=env, check=True, timeout=15)
    print(ROOT / "preview.png")


if __name__ == "__main__":
  main()
