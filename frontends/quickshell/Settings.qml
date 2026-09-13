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
    property int timeoutMs: 8000 // Zero keeps the latest caption until cleared.
    property int historyLimit: 40
    property int initialWidth: 900
    property int initialHeight: 520
    property int sourceFontSize: 28
    property int translationFontSize: 32
    property real backgroundOpacity: 0.78
}