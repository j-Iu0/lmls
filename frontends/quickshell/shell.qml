import QtQuick
import Quickshell
import Quickshell.Io

ShellRoot {
    id: root

    Settings { id: settings }

    SubtitleStream {
        id: stream
        url: settings.websocketUrl
        options: ({
            sourceLanguage: settings.sourceLanguage,
            targetLanguage: settings.targetLanguage,
            sourceTopics: settings.sourceTopics,
            translationTopics: settings.translationTopics,
            timeoutMs: settings.timeoutMs,
            historyLimit: settings.historyLimit
        })
    }

    SubtitleWindow {
        id: window
        screen: Quickshell.screens.find(s => s.name === settings.monitor)
            || Quickshell.screens[0] || null
        width: settings.initialWidth
        height: settings.initialHeight
        sourceText: stream.sourceText
        translationText: stream.translationText
        history: stream.history
        sourceFontSize: settings.sourceFontSize
        translationFontSize: settings.translationFontSize
        backgroundOpacity: settings.backgroundOpacity
    }

    IpcHandler {
        target: "subtitles"
        function show(): void { window.shown = true; }
        function hide(): void { window.shown = false; }
        function toggle(): void { window.shown = !window.shown; }
        function clear(): void { stream.clear(); }
        function quit(): void { Qt.quit(); }
        function status(): string {
            return JSON.stringify({
                connected: stream.connected,
                enabled: window.shown,
                visible: window.shown,
                monitor: window.screen ? window.screen.name : "",
                source: stream.sourceText,
                translation: stream.translationText,
                error: stream.lastError,
                rejectedMessages: stream.rejectedMessages
            });
        }
    }
}