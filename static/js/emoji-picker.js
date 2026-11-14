/**
 * State and behaviour behind templates/composer/partials/emoji_picker.html.
 *
 * Spread into an Alpine component — `{ ...emojiPicker('composer-emoji'), ... }`
 * — whose page renders the catalogue with `json_script` under that id
 * (apps.composer.emoji.emoji_catalogue). The component supplies
 * `insertEmoji(char)`, since only it knows which field has the cursor.
 */
window.emojiPicker = function (catalogueId) {
  const source = document.getElementById(catalogueId);
  return {
    emojiCategories: source ? JSON.parse(source.textContent) : [],
    showEmoji: false,
    emojiQuery: '',
    emojiCategory: 'smileys',

    toggleEmoji() {
      this.showEmoji = !this.showEmoji;
      if (this.showEmoji) {
        this.$nextTick(() => this.$refs.emojiSearch?.focus());
      }
    },

    visibleEmoji() {
      const query = this.emojiQuery.trim().toLowerCase();
      if (query) {
        // Search spans every category, capped so a one-letter query doesn't
        // render hundreds of cells.
        const hits = [];
        for (const cat of this.emojiCategories) {
          for (const item of cat.items) {
            if (item.n.includes(query)) {
              hits.push(item);
              if (hits.length >= 64) return hits;
            }
          }
        }
        return hits;
      }
      const cat = this.emojiCategories.find((c) => c.key === this.emojiCategory);
      return cat ? cat.items : [];
    },
  };
};
