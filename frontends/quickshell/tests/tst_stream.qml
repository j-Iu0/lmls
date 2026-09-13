import QtQuick
import QtTest
import QtWebSockets
import ".." as Overlay

TestCase {
    id: testCase
    name: "SubtitleStream"
    property var peer: null

    WebSocketServer {
        id: server
        host: "127.0.0.1"
        port: 0
        listen: true
        onClientConnected: function (socket) { testCase.peer = socket; }
    }

    Overlay.SubtitleStream {
        id: stream
        url: server.url
        options: ({sourceLanguage: "en", targetLanguage: "vi",
            sourceTopics: ["text.corrected", "text.raw"], translationTopics: [],
            timeoutMs: 8000, historyLimit: 20})
    }

    function test_receive() {
        tryCompare(stream, "connected", true);
        tryVerify(() => testCase.peer !== null);
        peer.sendTextMessage(JSON.stringify({type: "subtitle", topic: "text.raw",
            segment_id: "u1", revision: 0, lang: "en", text: "Live subtitle",
            t_audio_end: Date.now() / 1000, t_emit: Date.now() / 1000}));
        tryCompare(stream, "sourceText", "Live subtitle");
        tryCompare(stream, "historyCount", 1);
        compare(stream.history[0].source, "Live subtitle");
        peer.sendTextMessage("invalid JSON");
        tryCompare(stream, "rejectedMessages", 1);
        compare(stream.sourceText, "Live subtitle");

        peer.active = false;
        tryCompare(stream, "connected", false);
        tryCompare(stream, "connected", true, 5000);
        compare(stream.sourceText, "");
        peer.sendTextMessage(JSON.stringify({type: "subtitle", topic: "text.raw",
            segment_id: "u1", revision: 0, lang: "en", text: "Restarted session",
            t_audio_end: 1, t_emit: Date.now() / 1000}));
        tryCompare(stream, "sourceText", "Restarted session");
        stream.options = Object.assign({}, stream.options, {timeoutMs: 200});
        peer.sendTextMessage(JSON.stringify({type: "subtitle", topic: "text.raw",
            segment_id: "u2", revision: 0, lang: "en", text: "Brief caption",
            t_audio_end: 2, t_emit: Date.now() / 1000}));
        tryCompare(stream, "sourceText", "Brief caption");
        tryCompare(stream, "sourceText", "", 1500);
        compare(stream.history[0].source, "Brief caption");
    }
}
