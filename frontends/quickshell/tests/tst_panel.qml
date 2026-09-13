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

    function test_source_and_translation_toggles() {
        var sourceToggle = findChild(panel, "sourceToggle");
        var translationToggle = findChild(panel, "translationToggle");
        verify(sourceToggle.checked);
        verify(translationToggle.checked);
        mouseClick(sourceToggle);
        verify(!panel.showSource);
        verify(!findChild(panel, "liveSource").visible);
        verify(findChild(panel, "liveTranslation").visible);
        mouseClick(translationToggle);
        verify(!panel.showTranslation);
        verify(!findChild(panel, "liveTranslation").visible);
        mouseClick(sourceToggle);
        verify(panel.showSource);
        verify(findChild(panel, "liveSource").visible);
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
