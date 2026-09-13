import QtQuick
import QtTest
import ".." as Overlay

TestCase {
    id: testCase
    name: "WindowChrome"
    visible: true
    width: 1000
    height: 500
    when: windowShown

    Overlay.WindowChrome {
        id: chrome
        width: 900
        height: 460
        history: [{segmentId: "one", source: "Earlier source", translation: "Trước đó"}]
    }

    SignalSpy { id: quitSpy; target: chrome; signalName: "quitRequested" }
    SignalSpy { id: moveSpy; target: chrome; signalName: "moveRequested" }
    SignalSpy { id: resizeSpy; target: chrome; signalName: "resizeRequested" }

    function test_title_bar_press_requests_move() {
        mousePress(findChild(chrome, "titleBar"));
        compare(moveSpy.count, 1);
        mouseRelease(findChild(chrome, "titleBar"));
    }

    function test_resize_handles_request_edges() {
        mousePress(findChild(chrome, "resizeBottomRight"));
        compare(resizeSpy.signalArguments[0][0], Qt.BottomEdge | Qt.RightEdge);
        mouseRelease(findChild(chrome, "resizeBottomRight"));
        mousePress(findChild(chrome, "resizeLeft"));
        compare(resizeSpy.signalArguments[1][0], Qt.LeftEdge);
        mouseRelease(findChild(chrome, "resizeLeft"));
    }

    function test_hide_and_quit_controls() {
        var hideButton = findChild(chrome, "hideButton");
        mouseClick(hideButton);
        compare(chrome.shown, false);
        mouseClick(findChild(chrome, "showButton"));
        compare(chrome.shown, true);
        mouseClick(findChild(chrome, "quitButton"));
        compare(quitSpy.count, 1);
    }

    function test_content_shows_subtitle_history() {
        var list = findChild(chrome, "historyList");
        tryVerify(function () {
            return list.itemAtIndex(0) && findChild(list.itemAtIndex(0), "historySource");
        });
        compare(findChild(list.itemAtIndex(0), "historySource").text, "Earlier source");
        compare(findChild(list.itemAtIndex(0), "historyTranslation").text, "Trước đó");
    }

    function test_size_limits() {
        compare(chrome.minimumWidth, 480);
        compare(chrome.minimumHeight, 260);
        verify(chrome.maximumWidth > chrome.minimumWidth);
        verify(chrome.maximumHeight > chrome.minimumHeight);
    }
}