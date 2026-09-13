import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: root
    property var history: []
    property bool showSource: true
    property bool showTranslation: true
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property real backingOpacity: 0.78
    color: Qt.rgba(0.045, 0.075, 0.10, backingOpacity)
    radius: 14
    border.color: "#60748b9b"
    border.width: 1
    clip: true

    onHistoryChanged: {
        var atEnd = historyList.atYEnd;
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
        if (atEnd)
            historyList.positionViewAtEnd();
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
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#304f6374" }
        ListView {
            id: historyList
            objectName: "historyList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 16
            model: entries
            ScrollBar.vertical: ScrollBar { }
            delegate: Column {
                id: entry
                required property string segmentId
                required property string source
                required property string translation
                width: historyList.width - 16
                spacing: 6
                Text {
                    id: sourceText
                    objectName: "historySource"
                    width: parent.width
                    visible: root.showSource
                    text: entry.source
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#f1f5f8"
                    font.pixelSize: root.sourceFontSize
                }
                Text {
                    id: translationText
                    objectName: "historyTranslation"
                    width: parent.width
                    visible: root.showTranslation
                    text: entry.translation
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: "#ffe9a6"
                    font.pixelSize: root.translationFontSize
                }
                Rectangle { width: parent.width; height: 1; color: "#304f6374" }
            }
            Text {
                anchors.centerIn: parent
                visible: entries.count === 0
                text: "No subtitles yet"
                color: "#b0c0cc"
                font.pixelSize: 18
            }
        }
    }
}