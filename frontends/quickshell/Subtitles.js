// Plain JavaScript shared by QML and the Node test runner.
function createStore(options) {
    var blocks = new Map();
    var current = null;

    function line(block, language, topics) {
        var candidates = Array.from(block.lines.values()).filter(function (event) {
            return event.lang === language;
        });
        candidates.sort(function (a, b) {
            function rank(topic) {
                var index = topics.indexOf(topic);
                return index < 0 ? topics.length : index;
            }
            return rank(a.topic) - rank(b.topic);
        });
        return candidates.length ? candidates[0].text : "";
    }

    return {
        history: function () {
            return Array.from(blocks.values()).sort(function (a, b) { return a.time - b.time; })
                .map(function (block) {
                    return {segmentId: block.id,
                        source: line(block, options.sourceLanguage, options.sourceTopics),
                        translation: line(block, options.targetLanguage, options.translationTopics)};
                });
        },
        size: function () { return blocks.size; },
        reset: function () {
            blocks.clear();
            current = null;
        },
        receive: function (event, now) {
            if (!event || event.type !== "subtitle"
                    || typeof event.segment_id !== "string" || !event.segment_id
                    || typeof event.topic !== "string" || !event.topic
                    || typeof event.lang !== "string" || !event.lang
                    || typeof event.text !== "string"
                    || !Number.isInteger(event.revision) || event.revision < 0)
                return false;
            for (var field of ["t_emit", "t_audio_end"]) {
                if (event[field] !== undefined && (!Number.isFinite(event[field]) || event[field] < 0))
                    return false;
            }
            if (event.lang !== options.sourceLanguage && event.lang !== options.targetLanguage)
                return true;
            var block = blocks.get(event.segment_id);
            if (!block) {
                block = {id: event.segment_id, lines: new Map(),
                    time: event.t_audio_end || event.t_emit || now / 1000,
                    updatedAt: -Infinity};
                blocks.set(event.segment_id, block);
                if (!current || block.time >= current.time)
                    current = block;
                if (blocks.size > Math.max(1, options.historyLimit)) {
                    var oldest = Array.from(blocks.values()).sort(function (a, b) {
                        return a.time - b.time;
                    })[0];
                    blocks.delete(oldest.id);
                }
            }
            var key = JSON.stringify([event.topic, event.lang]);
            var previous = block.lines.get(key);
            if (previous && event.revision < previous.revision)
                return false;
            var before = [line(block, options.sourceLanguage, options.sourceTopics),
                line(block, options.targetLanguage, options.translationTopics)].join("\n");
            block.lines.set(key, event);
            var after = [line(block, options.sourceLanguage, options.sourceTopics),
                line(block, options.targetLanguage, options.translationTopics)].join("\n");
            if (before !== after)
                block.updatedAt = Math.max(block.updatedAt,
                    event.t_emit ? Math.min(now, event.t_emit * 1000) : now);
            return true;
        },
        view: function (now) {
            var active = current && (options.timeoutMs <= 0 || now - current.updatedAt < options.timeoutMs)
                ? current : null;
            return {
                segmentId: active ? active.id : "",
                source: active ? line(active, options.sourceLanguage, options.sourceTopics) : "",
                translation: active ? line(active, options.targetLanguage, options.translationTopics) : ""
            };
        }
    };
}
