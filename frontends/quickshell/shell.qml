import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

ShellRoot {
    id: root
    property bool shown: true

    Settings { id: settings }

    SubtitleStream {
        id: stream
        url: settings.websocketUrl
        options: ({
            sourceLanguage: settings.sourceLanguage,
            targetLanguage: settings.targetLanguage,
            sourceTopics: settings.sourceTopics,
            translationTopics: settings.translationTopics,
            timeoutMs: settings.timeoutMs,
            historyLimit: settings.historyLimit
        })
    }

    PanelWindow {
        id: panel
        screen: Quickshell.screens.find(s => s.name === settings.monitor)
            || Quickshell.screens[0] || null
        anchors.bottom: true
        margins.bottom: settings.bottomMargin
        implicitWidth: screen ? Math.min(settings.maxWidth, screen.width * settings.widthFraction) : 800
        implicitHeight: captions.implicitHeight + 32
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        mask: Region {}
        WlrLayershell.namespace: "lmls-subtitles"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        visible: root.shown && ((settings.showSource && stream.sourceText.length > 0)
            || (settings.showTranslation && stream.translationText.length > 0))

        Rectangle {
            anchors.fill: parent
            radius: 12
            color: Qt.rgba(0, 0, 0, Math.max(0, Math.min(1, settings.backgroundOpacity)))
        }

        Column {
            id: captions
            x: 20
            y: 16
            width: parent.width - 40
            spacing: 6

            Text {
                id: source
                width: parent.width
                visible: settings.showSource && text.length > 0
                text: stream.sourceText
                textFormat: Text.PlainText
                color: settings.sourceColor
                font.family: settings.fontFamily
                font.pixelSize: settings.sourceFontSize
                font.weight: Font.DemiBold
                style: Text.Outline
                styleColor: "#e0000000"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                maximumLineCount: settings.maxLines
                elide: Text.ElideRight
            }

            Text {
                id: translation
                width: parent.width
                visible: settings.showTranslation && text.length > 0
                text: stream.translationText
                textFormat: Text.PlainText
                color: settings.translationColor
                font.family: settings.fontFamily
                font.pixelSize: settings.translationFontSize
                font.weight: Font.DemiBold
                style: Text.Outline
                styleColor: "#e0000000"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                maximumLineCount: settings.maxLines
                elide: Text.ElideRight
            }
        }
    }

    IpcHandler {
        target: "subtitles"
        function show(): void { root.shown = true; }
        function hide(): void { root.shown = false; }
        function toggle(): void { root.shown = !root.shown; }
        function clear(): void { stream.clear(); }
        function quit(): void { Qt.quit(); }
        function status(): string {
            return JSON.stringify({
                connected: stream.connected,
                enabled: root.shown,
                visible: panel.visible,
                monitor: panel.screen ? panel.screen.name : "",
                source: stream.sourceText,
                translation: stream.translationText,
                error: stream.lastError,
                rejectedMessages: stream.rejectedMessages
            });
        }
    }
}
