// The "Custom" period of the period pills (components/_period_picker.html): an
// inline flatpickr range calendar whose two dates go into the form's hidden
// start and end fields. The root's data-* attributes carry the period shown
// (data-start, data-end) and the days that can be picked (data-min, data-max).

(function () {
    function iso(day) {
        return window.flatpickr.formatDate(day, 'Y-m-d');
    }

    window.periodPicker = function () {
        return {
            open: false,
            start: '',
            end: '',
            calendar: null,
            bounds: {},
            init() {
                var data = this.$el.dataset;
                this.start = data.start || '';
                this.end = data.end || '';
                this.bounds = { min: data.min || null, max: data.max || null };
            },
            toggle() {
                this.open = !this.open;
                if (this.open) this.$nextTick(() => this.mount());
            },
            mount() {
                if (this.calendar || !window.flatpickr) return;
                var lang = (document.documentElement.lang || '').toLowerCase();
                var months = window.innerWidth >= 640 ? 2 : 1;
                var self = this;
                this.calendar = window.flatpickr(this.$refs.calendar, {
                    inline: true,
                    mode: 'range',
                    dateFormat: 'Y-m-d',
                    minDate: this.bounds.min,
                    maxDate: this.bounds.max,
                    defaultDate: this.start && this.end ? [this.start, this.end] : null,
                    showMonths: months,
                    locale: lang.indexOf('pt') === 0 && window.flatpickr.l10ns.pt ? 'pt' : 'default',
                    onChange: function (dates) {
                        self.start = dates[0] ? iso(dates[0]) : '';
                        self.end = dates[1] ? iso(dates[1]) : '';
                    },
                });
                // Periods look back: the month of the end (or today) goes on the
                // right, the one before it on the left.
                if (months === 2) {
                    this.calendar.jumpToDate(this.end || this.bounds.max || new Date());
                    this.calendar.changeMonth(-1);
                }
            },
            shown(value) {
                if (!value) return '';
                var parts = value.split('-').map(Number);
                return new Date(parts[0], parts[1] - 1, parts[2]).toLocaleDateString(document.documentElement.lang || undefined, {
                    day: 'numeric', month: 'short', year: 'numeric',
                });
            },
        };
    };
})();
