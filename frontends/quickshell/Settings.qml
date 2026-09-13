import QtQuick
import Quickshell

QtObject {
    property url websocketUrl: Quickshell.env("LMLS_WS_URL") || "ws://127.0.0.1:8765"
    // Empty selects the first connected output. Use an output name from `niri msg outputs`.
    property string monitor: Quickshell.env("LMLS_MONITOR") || ""
    property string sourceLanguage: "en"
    property string targetLanguage: Quickshell.env("LMLS_LANGUAGE") || "vi"
    // Highest priority first. Names are wiring choices, not built-in stage names.
    property var sourceTopics: ["text.corrected", "text.raw"]
    property var translationTopics: ["text.out", "text.vi", "text.zh"]
    property bool showSource: true
    property bool showTranslation: true
    property int timeoutMs: 8000 // Zero keeps the latest caption until cleared.
    property int historyLimit: 40
    property int bottomMargin: 80
    property int maxWidth: 1100
    property real widthFraction: 0.8
    property string fontFamily: "sans-serif"
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property int maxLines: 3 // Per language; overflow is elided.
    property color sourceColor: "white"
    property color translationColor: "#ffe9a6"
    property real backgroundOpacity: 0.0
}
