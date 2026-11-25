// Reports what breaks in the page to the server (common.monitoring_views.client_error),
// which files it in the error tracker: uncaught errors, unhandled rejections,
// scripts and stylesheets that fail to load, swaps HTMX cannot do, and the
// page freezing. Loaded first, so it also sees the other scripts fail.
(function () {
    'use strict';

    var endpoint = document.currentScript.dataset.endpoint;
    // A page stuck in an error loop must not flood the server.
    var MAX_REPORTS = 5;
    // A main-thread task this long is a page that stopped answering clicks.
    var FREEZE_MS = 3000;
    var sent = 0;
    var seen = {};

    function report(kind, message, details) {
        message = String(message || '').slice(0, 300);
        var key = kind + '|' + message;
        if (!message || seen[key] || sent >= MAX_REPORTS) return;
        var token = document.querySelector('[name=csrfmiddlewaretoken]');
        if (!token) return;
        seen[key] = true;
        sent += 1;
        var body = Object.assign({ kind: kind, message: message, page: location.pathname }, details || {});
        try {
            fetch(endpoint, {
                method: 'POST',
                credentials: 'same-origin',
                keepalive: true,
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token.value },
                body: JSON.stringify(body),
            }).catch(function () {});
        } catch (_) { /* reporting must never break the page */ }
    }

    // Code the page did not ship: extensions, injected scripts.
    function foreign(source) {
        return /^(chrome|moz|safari|safari-web)-extension:/.test(source || '');
    }

    // Capture phase: load failures of <script> and <link> do not bubble.
    window.addEventListener('error', function (event) {
        var target = event.target;
        if (target && target !== window && target.tagName) {
            var tag = target.tagName;
            if (tag === 'SCRIPT' || tag === 'LINK') {
                report('resource', 'Failed to load ' + tag.toLowerCase(), { source: target.src || target.href || '' });
            }
            return;
        }
        // "Script error." is all a cross-origin script is allowed to say.
        if (!event.message || event.message === 'Script error.' || foreign(event.filename)) return;
        report('error', event.message, {
            source: event.filename || '',
            line: event.lineno || 0,
            column: event.colno || 0,
            stack: event.error && event.error.stack ? String(event.error.stack) : '',
        });
    }, true);

    window.addEventListener('unhandledrejection', function (event) {
        var reason = event.reason;
        var stack = reason && reason.stack ? String(reason.stack) : '';
        if (foreign(stack)) return;
        report('rejection', reason && reason.message ? reason.message : String(reason), { stack: stack });
    });

    // The request worked and the page could not use the answer: the element
    // to swap into is missing, or the swap itself threw.
    ['htmx:targetError', 'htmx:swapError', 'htmx:oobErrorNoTarget', 'htmx:onLoadError'].forEach(function (name) {
        document.addEventListener(name, function (event) {
            var detail = event.detail || {};
            var element = detail.elt || event.target;
            var where = element && element.tagName
                ? element.tagName.toLowerCase() + (element.id ? '#' + element.id : '')
                : '';
            var target = detail.target || (element && element.getAttribute && element.getAttribute('hx-target')) || '';
            report('htmx', name + (typeof target === 'string' && target ? ' ' + target : ''), {
                source: where,
                stack: detail.error && detail.error.stack ? String(detail.error.stack) : '',
            });
        });
    });

    if (window.PerformanceObserver && (PerformanceObserver.supportedEntryTypes || []).indexOf('longtask') !== -1) {
        new PerformanceObserver(function (list) {
            list.getEntries().forEach(function (entry) {
                if (entry.duration >= FREEZE_MS) {
                    // One message whatever the length, so freezes group together.
                    report('freeze', 'Page stopped responding', {
                        stack: 'Main thread blocked for ' + Math.round(entry.duration) + ' ms',
                    });
                }
            });
        }).observe({ entryTypes: ['longtask'] });
    }
})();
