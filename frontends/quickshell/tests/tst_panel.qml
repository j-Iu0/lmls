import QtQuick
import QtTest
import ".." as Overlay

TestCase {
    id: testCase
    name: "SubtitleControls"
    visible: true
    width: 1000
    height: 500
    when: windowShown

    Overlay.SubtitlePanel {
        id: panel
        width: 960
        height: 400
        history: [{segmentId: "one", source: "Earlier source", translation: "Trước đó"}]
    }

    function historyList() {
        return findChild(panel, "historyList");
    }

    function firstEntry() {
        var item = null;
        tryVerify(function () {
            item = historyList().itemAtIndex(0);
            return item && findChild(item, "historySource");
        });
        return item;
    }

    function test_font_sizes_and_backing_opacity() {
        panel.sourceFontSize = 20;
        panel.translationFontSize = 26;
        var first = firstEntry();
        compare(findChild(first, "historySource").font.pixelSize, 20);
        compare(findChild(first, "historyTranslation").font.pixelSize, 26);
        panel.backingOpacity = 0.5;
        compare(panel.color.a, 0.5);
    }

    function test_source_and_translation_toggles() {
        panel.showSource = true;
        panel.showTranslation = true;
        panel.history = [{segmentId: "one", source: "Earlier source", translation: "Trước đó"}];
        var first = firstEntry();
        var sourceToggle = findChild(panel, "sourceToggle");
        var translationToggle = findChild(panel, "translationToggle");
        verify(sourceToggle.checked);
        verify(translationToggle.checked);
        mouseClick(sourceToggle);
        verify(!panel.showSource);
        verify(!findChild(first, "historySource").visible);
        verify(findChild(first, "historyTranslation").visible);
        mouseClick(translationToggle);
        verify(!panel.showTranslation);
        verify(!findChild(first, "historyTranslation").visible);
        mouseClick(sourceToggle);
        verify(panel.showSource);
        verify(findChild(first, "historySource").visible);
    }

    function test_history_lists_segments_oldest_first() {
        panel.history = [
            {segmentId: "one", source: "First", translation: "Một"},
            {segmentId: "two", source: "Second", translation: "Hai"},
        ];
        var view = historyList();
        tryVerify(function () { return view.itemAtIndex(0) && view.itemAtIndex(1); });
        compare(view.itemAtIndex(0).segmentId, "one");
        compare(view.itemAtIndex(1).segmentId, "two");
    }

    function test_delay_labels_follow_their_lines() {
        panel.history = [
            {segmentId: "one", source: "First", translation: "Một",
                sourceDelay: 1234, translationDelay: 2500},
            {segmentId: "two", source: "Second", translation: "Hai"},
        ];
        var view = historyList();
        tryVerify(function () { return view.itemAtIndex(0) && view.itemAtIndex(1); });
        var withDelay = view.itemAtIndex(0);
        compare(findChild(withDelay, "historySourceDelay").text, "1.2 s");
        compare(findChild(withDelay, "historyTranslationDelay").text, "2.5 s");
        verify(findChild(withDelay, "historySourceDelay").visible);
        verify(findChild(withDelay, "historyTranslationDelay").visible);
        var noDelay = view.itemAtIndex(1);
        verify(!findChild(noDelay, "historySourceDelay").visible);
        verify(!findChild(noDelay, "historyTranslationDelay").visible);
        // Hiding a line hides its delay with it.
        mouseClick(findChild(panel, "sourceToggle"));
        verify(!findChild(withDelay = view.itemAtIndex(0), "historySourceDelay").visible);
    }

    function test_new_entries_autoscroll_when_at_bottom() {
        var view = historyList();
        var rows = [];
        for (var i = 0; i < 40; ++i)
            rows.push({segmentId: "s" + i, source: "s" + i, translation: "t" + i});
        panel.history = rows;
        tryVerify(function () { return view.atYEnd; });
        var pinned = view.contentY;
        var last = rows[rows.length - 1];
        panel.history = rows.slice(0, -1).concat([{
            segmentId: last.segmentId,
            source: last.source + " extended with a long trailing line of text that must wrap onto another line to grow the row height",
            translation: last.translation,
        }]);
        tryVerify(function () { return view.atYEnd && view.contentY > pinned; });
        view.contentY = 0;
        panel.history = panel.history.concat([{segmentId: "later", source: "later", translation: "sau"}]);
        compare(view.contentY, 0);
        view.positionViewAtEnd();
        panel.history = panel.history.concat([{segmentId: "later2", source: "later2", translation: "sau2"}]);
        tryVerify(function () { return view.atYEnd; });
    }
}