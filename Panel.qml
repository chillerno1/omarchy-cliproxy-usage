import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// CLIProxyAPI usage: one bar icon, one panel with a section per account
// behind the proxy and a meter per rate-limit window. All the fetching lives
// in bin/collector.py (python3, standard library); this file runs it and
// draws what it prints. Every displayed string is PlainText.
Panel {
  id: root
  moduleName: "chillerno1.cliproxy-usage"
  ipcTarget: "chillerno1.cliproxy-usage"
  manageIpc: false

  // The bar lays widgets out by their implicit size; without these the icon
  // gets a zero-height slot and draws clipped above the bar.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Commons.Color.popups.background
  readonly property color track: Style.selectedFillFor(foreground, Commons.Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string collector: decodeURIComponent(String(Qt.resolvedUrl("bin/collector.py")).replace(/^file:\/\//, ""))
  readonly property int refreshSeconds: Math.max(60, Number(setting("refreshIntervalSec", 600)) || 600)

  property var record: null
  property string loadError: ""
  property bool forcePending: false
  property double nowMs: Date.now()

  readonly property var accounts: record && record.accounts ? record.accounts : []
  // Only accounts with numbers to show get a section; the rest are named in
  // one line at the bottom, with why.
  readonly property var shownAccounts: accounts.filter(function(a) { return !a.disabled && a.limits && a.limits.length > 0 })
  readonly property var hiddenAccounts: accounts.filter(function(a) { return a.disabled || !a.limits || a.limits.length === 0 })
  readonly property bool proxyUp: !!(record && record.proxy && record.proxy.up)
  readonly property int proxyPort: record && record.proxy ? Number(record.proxy.port) || 8317 : 8317

  // Lit when a plain window (not a model-scoped one) on any live account is
  // nearly full, or the proxy itself is down.
  readonly property bool alarming: {
    if (record && !proxyUp) return true
    for (var i = 0; i < accounts.length; i++) {
      var a = accounts[i]
      if (a.disabled) continue
      var ls = a.limits || []
      for (var j = 0; j < ls.length; j++)
        if ((ls[j].title === "Session" || ls[j].title === "Weekly") && ls[j].percent >= 0.9) return true
    }
    return false
  }

  readonly property string heroPhrase: {
    if (loadError !== "") return loadError
    if (!record) return strings.t("loading")
    var live = shownAccounts.length
    var count = strings.tn("accounts", live).arg(Commons.I18n.formatNumber(live))
    return proxyUp
      ? strings.t("proxyUp").arg(proxyPort).arg(count)
      : strings.t("proxyDown").arg(proxyPort).arg(count)
  }

  // ------------------------------------------------------------- helpers

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  function colorLuminance(c) { return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b }

  function iconCandidates(provider) {
    var out = []
    if (colorLuminance(surface) >= 0.5) out.push(Qt.resolvedUrl("assets/" + provider + "-light.svg"))
    out.push(Qt.resolvedUrl("assets/" + provider + ".svg"))
    return out
  }

  function formatDuration(ms) {
    if (!(ms > 0)) return strings.t("now")
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return strings.t("daysHours").arg(Commons.I18n.formatNumber(days)).arg(Commons.I18n.formatNumber(hours % 24))
    if (hours > 0) return strings.t("hoursMinutes").arg(Commons.I18n.formatNumber(hours)).arg(Commons.I18n.formatNumber(minutes % 60))
    return strings.t("minutes").arg(Commons.I18n.formatNumber(Math.max(1, minutes)))
  }

  function windowTitle(title) {
    if (title === "Session") return strings.t("session")
    if (title === "Weekly") return strings.t("weekly")
    if (title === "Monthly") return strings.t("monthly")
    return String(title || "")
  }

  // ------------------------------------------------------------- data

  function runUpdate(force) {
    if (updateProcess.running) {
      if (force) forcePending = true
      return
    }
    updateProcess.command = force ? ["python3", "-I", collector, "--force"] : ["python3", "-I", collector]
    updateProcess.running = true
  }

  function refreshNow() { runUpdate(true) }

  function applyOutput(text) {
    var trimmed = String(text || "").trim()
    if (trimmed === "") return
    try {
      var parsed = JSON.parse(trimmed)
      if (!parsed || !Array.isArray(parsed.accounts)) throw new Error("bad record")
      record = parsed
      loadError = ""
    } catch (e) {
      loadError = strings.t("badOutput")
    }
  }

  Process {
    id: updateProcess
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyOutput(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("chl.cliproxy", text.trim())
    }
    onExited: function(code) {
      if (code !== 0 && !root.record) root.loadError = strings.t("collectorFailed")
      if (root.forcePending) {
        root.forcePending = false
        root.runUpdate(true)
      }
    }
  }

  Process {
    id: openProcess
    running: false
  }

  function openControlPanel() {
    openProcess.command = ["xdg-open", "http://127.0.0.1:" + proxyPort + "/management.html"]
    openProcess.running = true
  }

  // Background refresh; the collector reuses answers under two minutes old,
  // so the panel opening on top of this costs nothing extra.
  Timer {
    interval: root.refreshSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.runUpdate(false)
  }

  Timer {
    interval: 30000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  onOpenedChanged: {
    if (!opened) return
    nowMs = Date.now()
    runUpdate(false)
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
  }

  // ------------------------------------------------------------- bar

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰒍"
    active: root.alarming
    tooltipText: root.opened ? "" : root.heroPhrase
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openControlPanel()
      else if (buttonCode === Qt.MiddleButton) root.refreshNow()
      else root.toggle()
    }
  }

  // ------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(760))

    content: PanelKeyCatcher {
      id: keyCatcher
      implicitHeight: column.implicitHeight
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refreshNow()
        else if (t === "o" || t === "O") root.openControlPanel()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar {
          id: panelScroll
          policy: ScrollBar.AsNeeded
        }

        Column {
          id: column
          x: LayoutMirroring.enabled && panelFlick.interactive ? panelScroll.width + Style.space(6) : 0
          width: panelFlick.width - (panelFlick.interactive ? panelScroll.width + Style.space(6) : 0)
          spacing: Style.space(16)

          PanelHero {
            id: hero
            width: parent.width
            title: strings.t("title")
            meta: root.heroPhrase
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: button.text
                color: root.record && !root.proxyUp ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }

            trailingControl: Component {
              Row {
                spacing: Style.space(6)
                HeroButton {
                  glyph: "󰖟"
                  tooltip: strings.t("openControlPanel")
                  onClicked: root.openControlPanel()
                }
                HeroButton {
                  glyph: updateProcess.running ? "󰔟" : "󰑐"
                  tooltip: strings.t("refresh")
                  onClicked: root.refreshNow()
                }
              }
            }
          }

          Text {
            visible: root.record !== null && root.shownAccounts.length === 0
            width: parent.width
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            text: strings.t("noAccounts").arg(root.record && root.record.proxy ? String(root.record.proxy.authDir || "") : "")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: root.shownAccounts
            delegate: AccountSection {
              required property var modelData
              width: column.width
              account: modelData
            }
          }

          HiddenNote {
            width: column.width
          }
        }
      }
    }
  }

  // ------------------------------------------------------------- components

  component AccountSection: Column {
    id: section
    property var account: null
    readonly property var limits: account && account.limits ? account.limits : []
    readonly property bool stale: !!account && account.status !== "ok"
    spacing: Style.space(12)
    opacity: account && account.disabled ? 0.5 : 1.0

    PanelSeparator { foreground: root.foreground }

    Item {
      width: parent.width
      implicitHeight: Math.max(mark.height, name.implicitHeight)

      ProviderIcon {
        id: mark
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        provider: section.account ? section.account.provider : ""
      }

      Text {
        id: name
        anchors.left: mark.right
        anchors.leftMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: section.account ? section.account.providerName : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Text {
        anchors.left: name.right
        anchors.leftMargin: Style.space(8)
        anchors.right: plan.left
        anchors.rightMargin: Style.space(8)
        anchors.baseline: name.baseline
        textFormat: Text.PlainText
        text: section.account ? section.account.email : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideMiddle
      }

      Text {
        id: plan
        anchors.right: parent.right
        anchors.baseline: name.baseline
        textFormat: Text.PlainText
        text: section.account ? String(section.account.plan || "") : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Text {
      visible: text !== ""
      width: parent.width
      wrapMode: Text.Wrap
      textFormat: Text.PlainText
      text: {
        if (!section.account || section.account.note === "") return ""
        var note = section.account.note
        if (section.stale && section.limits.length > 0 && section.account.fetchedAt > 0)
          note += " · " + strings.t("lastUpdatedAgo").arg(root.formatDuration(root.nowMs - section.account.fetchedAt))
        return note
      }
      color: section.account && section.account.status === "stale" && section.limits.length === 0 ? root.urgent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Repeater {
      model: section.limits
      delegate: LimitRow {
        required property var modelData
        width: section.width
        window: modelData
        stale: section.stale
      }
    }

    Text {
      visible: text !== ""
      width: parent.width
      textFormat: Text.PlainText
      text: section.account ? String(section.account.extra || "") : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // One line per window: title, meter, time to reset. The exact percentage is
  // in the tooltip.
  component LimitRow: Item {
    id: row
    property var window: null
    property bool stale: false
    readonly property real percent: window ? Number(window.percent) : -1
    readonly property real resetMs: window && window.resetsAt > 0 ? window.resetsAt - root.nowMs : -1
    implicitHeight: Math.max(rowTitle.implicitHeight, rowValue.implicitHeight)
    opacity: stale ? 0.6 : 1.0

    HoverHandler { id: rowHover }

    PanelToolTip {
      visible: rowHover.hovered && !!row.window
      text: {
        if (!row.window) return ""
        var used = Commons.I18n.formatNumber(Math.round(row.percent * 100))
        return row.resetMs > 0
          ? strings.t("usedResets").arg(root.windowTitle(row.window.title)).arg(used).arg(root.formatDuration(row.resetMs))
          : strings.t("used").arg(root.windowTitle(row.window.title)).arg(used)
      }
    }

    Text {
      id: rowTitle
      width: parent.width * 0.3
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: row.window ? root.windowTitle(row.window.title) : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }

    Meter {
      anchors.left: rowTitle.right
      anchors.right: rowPct.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      value: row.percent
      alarming: row.percent >= 0.9
    }

    TextMetrics {
      id: pctMetrics
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      text: "100%"
    }

    Text {
      id: rowPct
      width: Math.ceil(pctMetrics.advanceWidth)
      anchors.right: rowValue.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      horizontalAlignment: Text.AlignRight
      text: row.percent >= 0 ? Commons.I18n.formatNumber(Math.round(row.percent * 100)) + "%" : ""
      color: row.percent >= 0.9 ? root.urgent : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    TextMetrics {
      id: valueMetrics
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      text: "00h 00m"
    }

    Text {
      id: rowValue
      width: Math.ceil(valueMetrics.advanceWidth)
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      horizontalAlignment: Text.AlignRight
      text: row.resetMs > 0 ? root.formatDuration(row.resetMs) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  // Accounts with nothing to show: disabled, a lapsed token, a failed first
  // check. One dim line names them; hovering it says why for each.
  component HiddenNote: Item {
    id: hiddenNote
    readonly property var list: root.hiddenAccounts
    readonly property int unsupported: root.record ? Number(root.record.unsupportedAccounts) || 0 : 0
    visible: list.length > 0 || unsupported > 0
    implicitHeight: visible ? hiddenText.implicitHeight + Style.space(4) : 0

    Text {
      id: hiddenText
      width: parent.width
      anchors.bottom: parent.bottom
      wrapMode: Text.Wrap
      textFormat: Text.PlainText
      text: {
        var names = []
        for (var i = 0; i < hiddenNote.list.length; i++) names.push(hiddenNote.list[i].providerName)
        var parts = []
        if (names.length > 0) parts.push(strings.t("hidden").arg(names.join(", ")))
        if (hiddenNote.unsupported > 0)
          parts.push(strings.tn("unsupported", hiddenNote.unsupported).arg(Commons.I18n.formatNumber(hiddenNote.unsupported)))
        return parts.join(" · ")
      }
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    HoverHandler { id: hiddenHover }

    PanelToolTip {
      visible: hiddenHover.hovered && hiddenNote.list.length > 0
      implicitWidth: Math.min(implicitContentWidth + leftPadding + rightPadding, Style.space(340))
      text: {
        var lines = []
        for (var i = 0; i < hiddenNote.list.length; i++) {
          var a = hiddenNote.list[i]
          lines.push(a.providerName + (a.email ? " (" + a.email + ")" : "") + ": " + (a.note || strings.t("noNumbers")))
        }
        return lines.join("\n")
      }
    }
  }

  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1)
      color: meter.alarming ? root.urgent : root.foreground
      Behavior on width {
        NumberAnimation { duration: Style.duration(160); easing.type: Easing.OutCubic }
      }
    }
  }

  component ProviderIcon: Item {
    id: icon
    property string provider: ""
    readonly property var candidates: provider !== "" ? root.iconCandidates(provider) : []
    property int candidateIndex: 0
    onProviderChanged: candidateIndex = 0
    property real size: Style.font.heading
    width: size
    height: size

    Image {
      id: iconImage
      anchors.fill: parent
      source: icon.candidateIndex < icon.candidates.length ? icon.candidates[icon.candidateIndex] : ""
      sourceSize.width: icon.size * 2
      sourceSize.height: icon.size * 2
      fillMode: Image.PreserveAspectFit
      onStatusChanged: if (status === Image.Error && icon.candidateIndex < icon.candidates.length)
        Qt.callLater(function() { icon.candidateIndex++ })
    }

    Text {
      anchors.centerIn: parent
      visible: iconImage.status !== Image.Ready
      textFormat: Text.PlainText
      text: button.text
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: icon.size
    }
  }

  component HeroButton: Rectangle {
    id: heroButton
    signal clicked()
    property string glyph: ""
    property string tooltip: ""
    implicitWidth: Style.space(34)
    implicitHeight: implicitWidth
    radius: Style.cornerRadius
    color: root.alpha(Commons.Color.accent, heroMouse.containsMouse ? 0.3 : 0.12)

    Text {
      anchors.centerIn: parent
      textFormat: Text.PlainText
      text: heroButton.glyph
      color: Commons.Color.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
    }

    MouseArea {
      id: heroMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: heroButton.clicked()
    }

    PanelToolTip {
      visible: heroMouse.containsMouse
      text: heroButton.tooltip
    }
  }
}
