// The task board's windows (tasks/board.html): the new task window and the
// task's own, which share their fields, and the art formats of "Request arts".
//
// They read three JSON blocks the board renders: "task-window-labels" (translated
// labels), "task-tag-suggestions" (the brand's tags) and "task-emoji" (the emoji
// catalogue).

(function () {
    function readJson(id, fallback) {
        var el = document.getElementById(id);
        return el ? JSON.parse(el.textContent) : fallback;
    }

    // The art formats of the "Request arts" switch (tasks/partials/_art_formats.html):
    // the network shown, how many formats are ticked per network and the
    // checklists kept in step with them (one per network).
    window.artPicker = function (firstTab) {
        return {
            tab: firstTab,
            picked: 0,
            counts: {},
            totals: {},
            pickedList: [],
            init() { this.recount(); },
            recount() {
                var totals = {};
                this.$root.querySelectorAll('input[name=formats]').forEach(function (box) {
                    totals[box.dataset.network] = (totals[box.dataset.network] || 0) + 1;
                });
                this.totals = totals;
                var counts = {};
                var list = [];
                var boxes = this.$root.querySelectorAll('input[name=formats]:checked');
                boxes.forEach(function (box) {
                    counts[box.dataset.network] = (counts[box.dataset.network] || 0) + 1;
                    list.push({ key: box.value, network: box.dataset.network, networkLabel: box.dataset.networkLabel, label: box.dataset.label, size: box.dataset.size });
                });
                this.counts = counts;
                this.picked = boxes.length;
                this.pickedList = list;
            },
            // The ticked formats by network, in the order the formats are listed.
            pickedGroups() {
                var groups = [];
                this.pickedList.forEach(function (art) {
                    var group = groups.find(function (g) { return g.network === art.network; });
                    if (!group) { group = { network: art.network, label: art.networkLabel, items: [] }; groups.push(group); }
                    group.items.push(art);
                });
                return groups;
            },
            // A network's box: ticks all its formats, or unticks them all when all are ticked; shows it.
            // Fixed formats (a task's arts already asked for, disabled) stay as they are.
            toggleNetwork(network) {
                var boxes = Array.from(this.$root.querySelectorAll('input[name=formats][data-network="' + network + '"]:not(:disabled)'));
                var all = boxes.length && boxes.every(function (box) { return box.checked; });
                boxes.forEach(function (box) { box.checked = !all; });
                this.tab = network;
                this.recount();
            },
            untick(key) {
                var box = this.$root.querySelector('input[name=formats][value="' + key + '"]');
                if (box) { box.checked = false; this.recount(); }
            },
        };
    };

    // State and behaviour of the task's fields (tasks/partials/_task_fields.html),
    // shared by the new task window and the task's own window. ``initial`` holds
    // the column, assignees, tags, channels and references to start from;
    // ``descLimit`` caps the description (0: no cap).
    window.taskFields = function (initial, descLimit) {
        var labels = readJson('task-window-labels', {});
        var suggestions = readJson('task-tag-suggestions', []);
        return Object.assign(window.emojiPicker('task-emoji'), window.tagGroupsBase(), {
            column: initial.column || '',
            descLength: 0,
            assignees: (initial.assignees || []).slice(),
            peopleOpen: false,
            tags: (initial.tags || []).slice(),
            tagQuery: '',
            tagsOpen: false,
            allTags: suggestions.slice(),
            // A task carries at most 5 tags (services.MAX_TASK_TAGS): networks ignore the rest.
            tagLimit: 5,
            // The art request's reference photos ({ id, url }), picked in the media library.
            references: (initial.references || []).slice(),
            // Which window asked the media library for references ("new" or "edit"): the
            // picked files come back to it only (referencePickerFor, on the page).
            referenceKey: initial.referenceKey || 'new',
            // The post's channels: { "<account id>": [formats] } (partials/_channel_picker.html).
            channels: Object.assign({}, initial.channels || {}),
            removeLabel: labels.remove || '',

            columnTone() {
                var select = this.$root.querySelector('select[name=column]');
                var option = select && Array.from(select.options).find(function (o) { return o.value === this.column; }, this);
                return option ? option.dataset.tone : 'brand';
            },

            pickReferences() {
                this.$dispatch('open-reference-picker', { for: this.referenceKey });
            },

            // The images picked (or uploaded) in the media library, once each, up to 10.
            addReferences(assets) {
                (assets || []).forEach(function (asset) {
                    var id = asset.asset_id || asset.id;
                    if (!id || (asset.media_type && asset.media_type !== 'image')) return;
                    if (this.references.length >= 10 || this.references.some(function (r) { return r.id === id; })) return;
                    this.references.push({ id: id, url: asset.thumbnail_url || asset.url || '' });
                }, this);
            },

            toggleChannelFormat(map, id, fmt) {
                var formats = (map[id] || []).slice();
                var at = formats.indexOf(fmt);
                if (at > -1) formats.splice(at, 1); else formats.push(fmt);
                if (formats.length) map[id] = formats; else delete map[id];
            },

            toggleAssignee(id) {
                var at = this.assignees.indexOf(id);
                if (at > -1) this.assignees.splice(at, 1); else this.assignees.push(id);
            },

            // The tag field (composer/partials/_tag_field.html): the brand's tags the search matches,
            // picking or dropping one, and a new one from the search (saved with the task).
            tagFiltered() {
                var q = this.tagQuery.trim().toLowerCase();
                return this.allTags.filter(function (t) { return !q || t.toLowerCase().indexOf(q) > -1; });
            },

            toggleTag(name) {
                var at = this.tags.indexOf(name);
                if (at > -1) this.tags.splice(at, 1);
                else if (!this.tagLimitReached(this.tags)) this.tags.push(name);
            },

            persistTag(name) {
                name = (name || '').trim().replace(/^#+/, '').trim().slice(0, 50);
                if (!name) return;
                if (this.allTags.indexOf(name) < 0) { this.allTags.push(name); this.allTags.sort(); }
                if (this.tags.indexOf(name) < 0 && !this.tagLimitReached(this.tags)) this.tags.push(name);
                this.tagQuery = '';
            },

            // The emoji goes where the cursor is in the description.
            insertEmoji(char) {
                var field = this.$refs.description;
                var start = field.selectionStart == null ? field.value.length : field.selectionStart;
                var end = field.selectionEnd == null ? start : field.selectionEnd;
                if (descLimit && field.value.length - (end - start) + char.length > descLimit) return;
                field.value = field.value.slice(0, start) + char + field.value.slice(end);
                field.selectionStart = field.selectionEnd = start + char.length;
                this.descLength = field.value.length;
                this.showEmoji = false;
                field.focus();
            },
        });
    };

    // The new task window (tasks/partials/_new_item_modal.html): the task's fields, its
    // checklists and "Request arts" (artPicker(): the network shown and the formats
    // ticked, by network). ``el`` is the window's root: its data-initial, when another page
    // sent something to the board (tasks.views._board_window), holds what the window opens
    // with on arrival: the fields' text, tags, channels, references, people, the two
    // switches, the arts to tick with how many and what each shows (artItems), its
    // "From …" note, and where it posts (submitTo) with what (hidden).
    window.newItemForm = function (firstTab, el) {
        var labels = readJson('task-window-labels', {});
        var pickerRecount = window.artPicker(firstTab).recount;
        var initial = JSON.parse((el && el.dataset.initial) || '{}');
        return Object.assign(window.taskFields(initial, 500), window.artPicker(firstTab), {
            isOpen: false,
            wantsArt: !!initial.wantsArt,
            wantsPost: !!initial.wantsPost,
            note: initial.note || '',
            submitTo: initial.submitTo || '',
            hidden: Object.assign({}, initial.hidden || {}),
            // Per ticked format: how many and what each shows ({ "<format>": { count, note } }),
            // posted as "art_items"; every ticked format has one (recount).
            artSpecs: {},
            checklists: [],
            nextKey: 1,
            checklistTitleLabel: labels.checklistTitle || '',
            itemPlaceholder: labels.checklistItem || '',

            init() {
                this.recount();
                if (initial.fields) this.open({ fields: initial.fields, artItems: initial.artItems });
            },

            // artPicker's count, then a count and note for each format just ticked.
            recount() {
                pickerRecount.call(this);
                var specs = this.artSpecs;
                this.pickedList.forEach(function (art) {
                    if (!specs[art.key]) specs[art.key] = { count: 1, note: '' };
                });
            },

            // What "art_items" posts: the ticked formats, each with its count and note.
            artItemsJson() {
                var specs = this.artSpecs;
                return JSON.stringify(this.pickedList.map(function (art) {
                    var spec = specs[art.key] || {};
                    return { format: art.key, count: spec.count || 1, note: spec.note || '' };
                }));
            },

            open(detail) {
                detail = detail || {};
                this.isOpen = true;
                // The column whose "Add task" opened the window (blank: the board's first one).
                this.column = detail.column || '';
                var self = this;
                // After x-show has shown the window: a hidden field can't take the focus.
                setTimeout(function () {
                    // Written into the fields rather than rendered in them, so the form's
                    // reset on closing leaves them blank for the next task.
                    var fields = detail.fields || {};
                    var form = self.$refs.taskForm;
                    Object.keys(fields).forEach(function (name) {
                        var field = form.querySelector('[name="' + name + '"]');
                        if (field && fields[name]) field.value = fields[name];
                    });
                    // The arts asked for: their formats ticked, the first one's network shown.
                    (detail.artItems || []).forEach(function (item) {
                        var box = form.querySelector('input[name=formats][value="' + item.format + '"]');
                        if (!box) return;
                        box.checked = true;
                        self.artSpecs[item.format] = { count: item.count || 1, note: item.note || '' };
                        if (detail.artItems[0] === item) self.tab = box.dataset.network;
                    });
                    self.recount();
                    self.descLength = self.$refs.description.value.length;
                    self.$refs.title.focus();
                }, 50);
            },

            close() {
                if (!this.isOpen) return;
                this.isOpen = false;
                this.showEmoji = false;
                this.$refs.taskForm.reset();
                this.descLength = 0;
                this.assignees = [];
                this.tags = [];
                this.tagQuery = '';
                this.checklists = [];
                this.wantsArt = false;
                this.wantsPost = false;
                this.channels = {};
                this.references = [];
                this.tab = firstTab;
                this.recount();
                // Once the checklist rows of the unticked formats are gone: they read
                // artSpecs[format] until then.
                this.$nextTick(function () { this.artSpecs = {}; }.bind(this));
                if (this.note || this.submitTo || initial.fields) {
                    // Done with what was sent: the next task is the board's own, and a reload
                    // doesn't open the window again.
                    this.note = '';
                    this.submitTo = '';
                    this.hidden = {};
                    initial = {};
                    var params = new URLSearchParams(location.search);
                    ['new', 'idea', 'template', 'variation', 'date', 'day'].forEach(function (key) { params.delete(key); });
                    var query = params.toString();
                    history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
                }
            },

            // A new named checklist, with its title field ready to type in.
            addChecklist() {
                if (this.checklists.length >= 10) return;
                var key = this.nextKey++;
                this.checklists.push({ key: key, title: '', items: [], text: '' });
                this.$nextTick(function () {
                    var field = document.getElementById('new-checklist-title-' + key);
                    if (field) field.focus();
                });
            },

            // An item: its text, how many (count: that many numbered items, as an art asked
            // for several times) and its settings (the gear: ask for a file, the file's size).
            addItem(list) {
                var text = list.text.trim();
                if (text && list.items.length < 50) list.items.push({ text: text.slice(0, 255), count: 1, accepts_file: false, width: '', height: '' });
                list.text = '';
            },

            // What the "checklists" field posts: [{ title, items }], the item typed but not added included;
            // each item is its text, or { text, count, accepts_file, width, height } when it has a count
            // above one or settings.
            checklistsJson() {
                return JSON.stringify(this.checklists.map(function (list) {
                    var items = list.items.map(function (item) {
                        return item.count > 1 || item.accepts_file || (item.width && item.height) ? item : item.text;
                    });
                    if (list.text.trim()) items.push(list.text.trim().slice(0, 255));
                    return { title: list.title.trim(), items: items };
                }));
            },
        });
    };
})();
