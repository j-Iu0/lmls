import QtQuick
import QtTest
import ".." as Overlay

TestCase {
    id: testCase
    name: "SubtitleControls"
    width: 1000
    height: 500
    when: windowShown

    Overlay.SubtitlePanel {
        id: panel
        width: 960
        height: 400
        sourceText: "Current source"
        translationText: "Bản dịch"
        history: [{segmentId: "one", source: "Earlier source", translation: "Trước đó"}]
    }

    function test_history() {
        mouseClick(findChild(panel, "historyButton"));
        compare(panel.viewingHistory, true);
        verify(findChild(panel, "historyList").visible);
        panel.sourceText = "New live source";
        panel.history = panel.history.concat([{segmentId: "two", source: "New live source", translation: "Mới"}]);
        compare(panel.viewingHistory, true);
        mouseClick(findChild(panel, "historyButton"));
        compare(panel.viewingHistory, false);
        compare(findChild(panel, "liveSource").text, "New live source");
    }
}
