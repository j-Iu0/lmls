import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: root
    property string sourceText: ""
    property string translationText: ""
    property var history: []
    property bool viewingHistory: false
    color: Qt.rgba(0.045, 0.075, 0.10, 0.78)
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
                    text: root.sourceText
                    color: "#f1f5f8"
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: 28
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    width: parent.width
                    text: root.translationText
                    color: "#ffe9a6"
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    font.pixelSize: 32
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
