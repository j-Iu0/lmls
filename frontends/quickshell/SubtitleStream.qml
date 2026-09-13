import QtQuick
import QtWebSockets
import "Subtitles.js" as Subtitles

Item {
    id: root
    required property url url
    required property var options
    readonly property bool connected: socket.status === WebSocket.Open
    readonly property string sourceText: display.source
    readonly property string translationText: display.translation
    property var store: Subtitles.createStore(options)
    property var display: ({segmentId: "", source: "", translation: ""})
    property int rejectedMessages: 0
    property string lastError: ""
    property int retryDelay: 500
    property var history: []
    readonly property int historyCount: history.length
    onStoreChanged: history = store.history()

    function refresh() { display = store.view(Date.now()); }
    function clear() {
        store.reset();
        history = store.history();
        refresh();
    }

    Timer {
        interval: 100
        running: true
        repeat: true
        onTriggered: root.refresh()
    }

    Timer {
        id: reconnect
        interval: root.retryDelay
        onTriggered: {
            root.retryDelay = Math.min(root.retryDelay * 2, 10000);
            socket.active = true;
        }
    }

    WebSocket {
        id: socket
        url: root.url
        active: true
        onStatusChanged: function (status) {
            if (status === WebSocket.Open) {
                reconnect.stop();
                root.retryDelay = 500;
                root.lastError = "";
                root.clear();
            } else if (status === WebSocket.Error || status === WebSocket.Closed) {
                if (status === WebSocket.Error)
                    root.lastError = errorString;
                socket.active = false;
                if (!reconnect.running)
                    reconnect.start();
            }
        }
        onTextMessageReceived: function (message) {
            try {
                var event = JSON.parse(message);
                if (!root.store.receive(event, Date.now())) {
                    root.rejectedMessages++;
                    if (event && event.type === "subtitle" && !event.topic)
                        root.lastError = "Subtitle event lacks topic; update the lmls backend.";
                }
                root.refresh();
                root.history = root.store.history();
            } catch (error) {
                root.rejectedMessages++;
                root.lastError = "Invalid subtitle JSON";
            }
        }
    }
}
