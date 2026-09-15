import QtQuick
import Quickshell

QtObject {
    property url websocketUrl: Quickshell.env("LMLS_WS_URL") || "ws://127.0.0.1:8765"
    // Empty selects the first connected output. Use an output name from `niri msg outputs`.
    property string monitor: Quickshell.env("LMLS_MONITOR") || ""
    property string sourceLanguage: "en"
    // Input-port names of the subtitle sink; highest priority first.
    property var sourcePorts: ["corrected", "raw"]
    property var translationPorts: ["translated"]
    property int timeoutMs: 8000 // Zero keeps the latest caption until cleared.
    property int historyLimit: 40
    property int initialWidth: 900
    property int initialHeight: 520
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property real backgroundOpacity: 0.78
}