import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    property string hint: text
    implicitHeight: 32
    implicitWidth: label.implicitWidth + 22
    hoverEnabled: true
    focusPolicy: Qt.NoFocus
    ToolTip.visible: hovered
    ToolTip.text: hint
    ToolTip.delay: 600
    contentItem: Text {
        id: label
        text: control.text
        color: control.enabled ? (control.checked ? "#c7eaff" : "#e4edf3") : "#667580"
        font.pixelSize: 13
        font.weight: Font.Medium
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        radius: 6
        color: control.down ? "#50677b" : control.checked ? "#3b5367" : control.hovered ? "#354757" : "#20313f"
        border.color: control.checked ? "#7eafcc" : "#405361"
        opacity: control.enabled ? 1 : 0.6
    }
}
