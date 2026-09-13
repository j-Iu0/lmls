import QtQuick

Item {
    id: root
    property bool shown: true
    property string sourceText: ""
    property string translationText: ""
    property var history: []
    property int minimumWidth: 480
    property int minimumHeight: 260
    property int maximumWidth: 1600
    property int maximumHeight: 1000
    signal quitRequested()
    signal moveRequested()
    signal resizeRequested(var edges)

    Rectangle {
        id: titleBar
        objectName: "titleBar"
        width: parent.width
        height: 40
        color: "#1c2b3a"

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.SizeAllCursor
            onPressed: root.moveRequested()
        }

        Text {
            anchors.centerIn: parent
            text: "LMLS Subtitles"
            color: "#e4edf3"
            font.pixelSize: 13
            font.weight: Font.Medium
        }

        OverlayButton {
            objectName: "showButton"
            text: "Show"
            visible: !root.shown
            anchors.verticalCenter: parent.verticalCenter
            anchors.right: hideButton.left
            anchors.rightMargin: 8
            onClicked: root.shown = true;
        }
        OverlayButton {
            id: hideButton
            objectName: "hideButton"
            text: "Hide"
            anchors.verticalCenter: parent.verticalCenter
            anchors.right: quitButton.left
            anchors.rightMargin: 8
            onClicked: root.shown = false;
        }
        OverlayButton {
            id: quitButton
            objectName: "quitButton"
            text: "Quit"
            anchors.verticalCenter: parent.verticalCenter
            anchors.right: parent.right
            anchors.rightMargin: 12
            onClicked: root.quitRequested()
        }
    }

    SubtitlePanel {
        id: panel
        y: 40
        width: parent.width
        height: parent.height - 40
        visible: root.shown
        sourceText: root.sourceText
        translationText: root.translationText
        history: root.history
    }

    MouseArea {
        objectName: "resizeLeft"
        x: -2; y: 12
        width: 6
        height: parent.height - 24
        cursorShape: Qt.SizeHorCursor
        onPressed: root.resizeRequested(Qt.LeftEdge)
    }
    MouseArea {
        objectName: "resizeRight"
        x: parent.width - 4
        y: 12
        width: 6
        height: parent.height - 24
        cursorShape: Qt.SizeHorCursor
        onPressed: root.resizeRequested(Qt.RightEdge)
    }
    MouseArea {
        objectName: "resizeTop"
        x: 12
        y: -2
        width: parent.width - 24
        height: 6
        cursorShape: Qt.SizeVerCursor
        onPressed: root.resizeRequested(Qt.TopEdge)
    }
    MouseArea {
        objectName: "resizeBottom"
        x: 12
        y: parent.height - 4
        width: parent.width - 24
        height: 6
        cursorShape: Qt.SizeVerCursor
        onPressed: root.resizeRequested(Qt.BottomEdge)
    }
    MouseArea {
        objectName: "resizeTopLeft"
        x: -2; y: -2
        width: 12; height: 12
        cursorShape: Qt.SizeFDiagCursor
        onPressed: root.resizeRequested(Qt.LeftEdge | Qt.TopEdge)
    }
    MouseArea {
        objectName: "resizeTopRight"
        x: parent.width - 10; y: -2
        width: 12; height: 12
        cursorShape: Qt.SizeBDiagCursor
        onPressed: root.resizeRequested(Qt.RightEdge | Qt.TopEdge)
    }
    MouseArea {
        objectName: "resizeBottomLeft"
        x: -2; y: parent.height - 10
        width: 12; height: 12
        cursorShape: Qt.SizeBDiagCursor
        onPressed: root.resizeRequested(Qt.LeftEdge | Qt.BottomEdge)
    }
    MouseArea {
        objectName: "resizeBottomRight"
        x: parent.width - 10
        y: parent.height - 10
        width: 12
        height: 12
        cursorShape: Qt.SizeFDiagCursor
        onPressed: root.resizeRequested(Qt.RightEdge | Qt.BottomEdge)
    }
}