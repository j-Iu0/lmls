import QtQuick
import Quickshell

FloatingWindow {
    id: root
    property bool shown: true
    property string sourceText: ""
    property string translationText: ""
    property var history: []
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property real backgroundOpacity: 0.78
    signal quitRequested()

    title: "LMLS Subtitles"
    visible: shown
    color: "transparent"
    minimumSize: Qt.size(chrome.minimumWidth, chrome.minimumHeight)
    maximumSize: Qt.size(chrome.maximumWidth, chrome.maximumHeight)

    WindowChrome {
        id: chrome
        anchors.fill: parent
        shown: root.shown
        sourceText: root.sourceText
        translationText: root.translationText
        history: root.history
        sourceFontSize: root.sourceFontSize
        translationFontSize: root.translationFontSize
        backgroundOpacity: root.backgroundOpacity

        onMoveRequested: root.startSystemMove()
        onResizeRequested: edges => root.startSystemResize(edges)
    }

    onQuitRequested: Qt.quit()
}