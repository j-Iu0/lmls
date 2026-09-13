import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: root
    property string sourceText: ""
    property string translationText: ""
    property var history: []
    property bool viewingHistory: false
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property real backingOpacity: 0.78
    property bool showSource: true
    property bool showTranslation: true
    color: Qt.rgba(0.045, 0.075, 0.10, backingOpacity)
    radius: 14
    border.color: "#60748b9b"
    border.width: 1
    clip: true

    onHistoryChanged: {
        // Keep existing delegates/scroll position when incoming text updates history.
        for (var i = entries.count - 1; i >= 0; --i) {
            if (!history.some(row => row.segmentId === entries.get(i).segmentId))
                entries.remove(i);
        }
        for (var j = 0; j < history.length; ++j) {
            if (j >= entries.count || entries.get(j).segmentId !== history[j].segmentId)
                entries.insert(j, history[j]);
            else
                entries.set(j, history[j]);
        }
    }
    ListModel { id: entries }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "LMLS"
                color: "#e4edf3"
                font.pixelSize: 14
                font.weight: Font.Bold
                Layout.fillWidth: true
            }
            OverlayButton {
                objectName: "sourceToggle"
                text: "Source"
                checkable: true
                checked: root.showSource
                onClicked: root.showSource = checked;
            }
            OverlayButton {
                objectName: "translationToggle"
                text: "Translation"
                checkable: true
                checked: root.showTranslation
                onClicked: root.showTranslation = checked;
            }
            OverlayButton {
                objectName: "historyButton"
                text: root.viewingHistory ? "Return to live" : "History · " + entries.count
                checked: root.viewingHistory
                onClicked: {
                    root.viewingHistory = !root.viewingHistory;
                    if (root.viewingHistory)
                        historyList.positionViewAtEnd();
                }
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#304f6374" }
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !root.viewingHistory
            contentWidth: availableWidth
            Column {
                width: parent.width
                spacing: 8
                Text {
                    objectName: "liveSource"
                    width: parent.width
                    visible: root.showSource && text.length > 0
                    text: root.sourceText
                    color: "#f1f5f8"
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: root.sourceFontSize
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    objectName: "liveTranslation"
                    width: parent.width
                    visible: root.showTranslation && text.length > 0
                    text: root.translationText
                    color: "#ffe9a6"
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: root.translationFontSize
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    width: parent.width
                    visible: !root.sourceText && !root.translationText
                    text: "Waiting for subtitles…"
                    color: "#b0c0cc"
                    font.pixelSize: 18
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }
        ListView {
            id: historyList
            objectName: "historyList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.viewingHistory
            clip: true
            spacing: 16
            model: entries
            ScrollBar.vertical: ScrollBar { }
            delegate: Column {
                required property string source
                required property string translation
                width: historyList.width - 16
                spacing: 6
                Text {
                    width: parent.width
                    text: parent.source
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#f1f5f8"
                    font.pixelSize: 24
                }
                Text {
                    width: parent.width
                    text: parent.translation
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#ffe9a6"
                    font.pixelSize: 26
                }
                Rectangle { width: parent.width; height: 1; color: "#304f6374" }
            }
            Text {
                anchors.centerIn: parent
                visible: entries.count === 0
                text: "No subtitle history yet"
                color: "#b0c0cc"
                font.pixelSize: 18
            }
        }
    }
}
