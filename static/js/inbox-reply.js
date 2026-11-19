/**
 * The inbox reply box (templates/inbox/partials/_reply_composer.html).
 *
 * Loaded once by the inbox pages rather than inside the partial: the partial
 * arrives through htmx swaps, and the component has to exist before Alpine
 * initialises the swapped markup. Needs static/js/emoji-picker.js and the
 * `inbox-emoji` json_script on the page.
 *
 * options: { limit, canSend, firstName }. `limit` is the platform's maximum
 * reply length in characters (0 when unknown).
 *
 * `quote` is the customer's message the reply quotes ({ id, name, text }),
 * picked on its bubble: the bubble's menu (inboxMessageMenu, below) announces
 * it with an `inbox-quote` window event, since the two live in different
 * Alpine scopes.
 */
window.inboxReply = function (options) {
  return {
    ...window.emojiPicker('inbox-emoji'),
    mode: 'reply',
    noteText: '',
    replyText: '',
    limit: options.limit || 0,
    canSend: Boolean(options.canSend),
    firstName: options.firstName || '',
    savedOpen: false,
    savedQuery: '',
    quote: null,

    setQuote(detail) {
      this.mode = 'reply';
      this.quote = detail;
      this.$nextTick(() => { if (this.$refs.reply) this.$refs.reply.focus(); });
    },

    get count() { return [...this.replyText].length; },
    get over() { return this.limit > 0 && this.count > this.limit; },
    get sendable() { return this.canSend && !this.over && this.replyText.trim().length > 0; },

    field() { return this.mode === 'note' ? this.$refs.note : this.$refs.reply; },

    /* Insert at the cursor of whichever box is showing. */
    insert(text) {
      const key = this.mode === 'note' ? 'noteText' : 'replyText';
      const el = this.field();
      const value = this[key];
      const start = el ? (el.selectionStart ?? value.length) : value.length;
      const end = el ? (el.selectionEnd ?? start) : start;
      this[key] = value.slice(0, start) + text + value.slice(end);
      this.$nextTick(() => {
        if (!el) return;
        el.focus();
        const at = start + text.length;
        el.setSelectionRange(at, at);
        this.grow(el);
      });
    },

    insertEmoji(char) { this.insert(char); },

    useSaved(body) {
      this.mode = 'reply';
      this.replyText = this.replyText.trim() ? this.replyText.replace(/\s*$/, '\n') + body : body;
      this.savedOpen = false;
      this.savedQuery = '';
      this.$nextTick(() => { this.$refs.reply.focus(); this.grow(this.$refs.reply); });
    },

    savedMatches(el) {
      const q = this.savedQuery.trim().toLowerCase();
      return !q || el.dataset.search.includes(q);
    },

    /* The box grows with its text up to 12rem, then scrolls. */
    grow(el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 192) + 'px';
    },

    /* Enter sends, Shift+Enter breaks the line. IME composition is left alone. */
    onEnter(event) {
      if (event.shiftKey || event.isComposing) return;
      event.preventDefault();
      if (this.mode === 'reply' && !this.sendable) return;
      if (this.mode === 'note' && !this.noteText.trim()) return;
      event.target.form.requestSubmit();
    },

    reset(el) {
      el.style.height = '';
    },
  };
};

/**
 * The menu on a chat bubble (templates/inbox/partials/_chat_incoming.html).
 * The buttons carry the message in data-* attributes: its text never goes
 * into an Alpine expression.
 */
window.inboxMessageMenu = function () {
  return {
    open: false,

    quote(el) {
      this.$dispatch('inbox-quote', { id: el.dataset.id, name: el.dataset.name, text: el.dataset.text });
      this.open = false;
    },

    copy(el) {
      this.open = false;
      navigator.clipboard.writeText(el.dataset.text).then(() => {
        document.body.dispatchEvent(new CustomEvent('showToast', { detail: { tone: 'success', title: el.dataset.done } }));
      });
    },
  };
};
