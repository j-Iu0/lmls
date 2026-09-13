import QtQuick
import QtTest
import ".." as Overlay

TestCase {
    visible: true
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

    function test_font_sizes_and_backing_opacity() {
        panel.sourceFontSize = 20;
        panel.translationFontSize = 26;
        compare(findChild(panel, "liveSource").font.pixelSize, 20);
        compare(findChild(panel, "liveTranslation").font.pixelSize, 26);
        panel.backingOpacity = 0.5;
        compare(panel.color.a, 0.5);
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
