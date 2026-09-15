// Plain JavaScript shared by QML and the Node test runner.
function createStore(options) {
    var blocks = new Map();
    var current = null;

    // The event chosen by input port and language; its text and its own end-to-end
    // delay are displayed together. The source line requires the configured source
    // language; the translated line accepts any language (a null language) and the
    // displayed language badge reports which one arrived.
    function line(block, language, ports) {
        var candidates = Array.from(block.lines.values()).filter(function (event) {
            return ports.indexOf(event.port) >= 0
                && (language === null || event.lang === language);
        });
        candidates.sort(function (a, b) {
            function rank(port) {
                var index = ports.indexOf(port);
                return index < 0 ? ports.length : index;
            }
            return rank(a.port) - rank(b.port);
        });
        return candidates[0] || null;
    }

    function text(block, language, ports) {
        var chosen = block ? line(block, language, ports) : null;
        return chosen ? chosen.text : "";
    }

    function delay(block, language, ports) {
        var chosen = block ? line(block, language, ports) : null;
        return chosen && Number.isFinite(chosen.end_to_end_ms) ? chosen.end_to_end_ms : null;
    }

    function languageOf(block, language, ports) {
        var chosen = block ? line(block, language, ports) : null;
        return chosen ? chosen.lang : "";
    }

    return {
        history: function () {
            return Array.from(blocks.values()).sort(function (a, b) { return a.time - b.time; })
                .map(function (block) {
                    return {segmentId: block.id,
                        source: text(block, options.sourceLanguage, options.sourcePorts),
                    sourceDelay: delay(block, options.sourceLanguage, options.sourcePorts),
                    translation: text(block, null, options.translationPorts),
                    translationLang: languageOf(block, null, options.translationPorts),
                    translationDelay: delay(block, null, options.translationPorts)};
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
                    || typeof event.port !== "string" || !event.port
                    || typeof event.lang !== "string" || !event.lang
                    || typeof event.text !== "string"
                    || !Number.isInteger(event.revision) || event.revision < 0)
                return false;
            for (var field of ["t_emit", "t_audio_end"]) {
                if (event[field] !== undefined && (!Number.isFinite(event[field]) || event[field] < 0))
                    return false;
            }
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
            var key = JSON.stringify([event.port, event.lang]);
            var previous = block.lines.get(key);
            if (previous && event.revision < previous.revision)
                return false;
            var before = [text(block, options.sourceLanguage, options.sourcePorts),
                text(block, null, options.translationPorts)].join("\n");
            block.lines.set(key, event);
            var after = [text(block, options.sourceLanguage, options.sourcePorts),
                text(block, null, options.translationPorts)].join("\n");
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
                source: text(active, options.sourceLanguage, options.sourcePorts),
                sourceDelay: delay(active, options.sourceLanguage, options.sourcePorts),
                translation: text(active, null, options.translationPorts),
                translationLang: languageOf(active, null, options.translationPorts),
                translationDelay: delay(active, null, options.translationPorts)
            };
        }
    };
}
