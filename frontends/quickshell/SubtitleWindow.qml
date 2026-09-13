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

    visible: shown
    color: "transparent"
    minimumWidth: chrome.minimumWidth
    minimumHeight: chrome.minimumHeight
    maximumWidth: chrome.maximumWidth
    maximumHeight: chrome.maximumHeight

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