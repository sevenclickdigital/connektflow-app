---
name: django-templates
description: "Use when creating or editing Django templates, HTMX interactions, Alpine.js components or user-facing strings in templates/JS in this project. Covers partial layout, the HX-Trigger/showToast contract, translation in templates and JavaScript (json_script), Tailwind/token classes, and the template traps that already shipped (multi-line {# #}, missing load i18n, fuzzy catalogue entries, text selection on drag)."
---

# Templates, HTMX and Alpine in Connekt Flow

## Layout

- Pages extend `base.html`; app pages live in `templates/<app>/`, HTMX fragments in `templates/<app>/partials/_name.html`.
- Shared pieces: `templates/components/` and `templates/partials/`. Search them before writing markup (`reuse-check`).
- **Page spacing is the base layout's, never the page's own.** `base.html` gives `<main>` its padding (`px-4 py-5 sm:px-6 lg:px-8 lg:py-8`) and the content column (`w-full max-w-7xl mx-auto`). Pages don't override `main_padding` or `content_width` and don't add their own outer padding: the title starts where every other page's title starts (reference: `templates/members/list.html`). Only the flow builder canvas empties both blocks; `account_select.html` narrows the column to `max-w-5xl`. Every page scrolls as a whole, like a web page: `<main>` scrolls, with the scrollbar at the window's edge, never a box beside the content column (users read a scrollbar inside the column as broken). Don't give a page its own scrolling wrapper. Only app-like screens whose panels scroll on their own (inbox `feed.html` / `message_detail.html`, `flow_builder.html`, `flow_new.html`, `composer/compose.html`) bound the column to the window by setting `column_height` to `min-h-0` and `content_scroll` to `min-h-0 overflow-y-auto overflow-x-hidden`. A modal whose body scrolls inside a `max-h` flex-column panel needs every element between panel and body (a `<form>` included) to be `flex flex-col flex-1 min-h-0`, or the body never scrolls (the idea modal shipped broken that way).
- **Page header:** `<div class="page-header">` with `<h1 class="page-title">` and `<p class="page-description">`, actions on the right — in `{% block page_header %}`, or at the top of `content` when it needs the page's Alpine state (the composer). No back buttons in page headers. `.page-header` is an unlayered component class, so Tailwind utilities on the same element (`mb-0`, `items-center`) do not override it.
- **E-mails** extend `emails/base.html`; the title goes in `{% block heading %}`, never as an `<h1>` in `content`. The layout places it beside the mascot on wide screens and above the text on phones; an `<h1>` in the content leaves an empty gap under the header. Text bodies (`.txt`) carry no heading block.
- Error pages (`templates/errors/`, `400/403/404/500.html`) deliberately extend nothing and load no stylesheet — Django may render them with an empty context.

## HTMX contract

- Mutations are `POST` with CSRF (`base.html` adds `X-CSRFToken` to every HTMX request from its `{% csrf_token %}`).
- A state-changing action answers **204 + `HX-Trigger`**: `{"showToast": {"message": ..., "tone": "success|warn|error"}, "<refresh-event>": ...}`. Reference: `apps/approvals/views.py` and `apps/approvals/test_workflow.py`.
- Use `request.htmx` to return a partial for HTMX and the full page otherwise.
- Failures must reach the user: an error toast or an inline error partial, never a silent 200.
- **Slow parts load after the page.** When a page waits on a platform's API or a heavy calculation, render the page at once and put the slow part in a placeholder: `<div hx-get="{{ body_url }}" hx-trigger="load" hx-swap="outerHTML" data-inline-error="#lazy-load-status">{% include "components/_skeleton.html" with variant=… label=… %}</div>`. `apps.common.htmx.lazy_body_urls(request, part)` gives `body_url` (the page's URL with `partial=<part>`) and `page_url` (for a "Try again" inside the part); `wants_lazy_body(request)` tells the view it is that request. If the part has its own id that other controls swap (filters, sorting), give the placeholder that id. References: `templates/ads/index.html`, `templates/analytics/_meta_ads_page.html`, `apps/workspaces/views.py` (`partial=networks`). Only for slow parts: a page that reads the database quickly would just flash. Not for anything that decides what the person may do (a subscription check that prevents paying twice), which must run before the page shows.
- Confirmations go through the app's dialog (`templates/components/_confirm_dialog.html`, included by `base.html`): `hx-confirm="…"` on an HTMX element, or `data-confirm="…"` on a plain form; add `data-confirm-tone="danger"` and `data-confirm-label` for deletions. Never `confirm()` / `onsubmit="return confirm(…)"`, which opens the browser's own box. Keep the question short (it is set as a title) and put what will happen in `data-confirm-body`.

## Translation — every user-visible string

- `{% load i18n %}` in **every** template that uses `{% translate %}`/`{% blocktranslate %}`. Loads are not inherited by children or includes.
- Variables: `{% blocktranslate with name=account.name %}Connected {{ name }}.{% endblocktranslate %}` — never concatenate.
- Attributes: `placeholder="{% translate 'Search' %}"`. `aria-label`, `title`, `alt` and `data-*` attributes shown to the user count as visible text.
- **JavaScript:** a string literal in JS is invisible to `makemessages`. Translate in the template and pass it in:
  ```django
  {{ labels|json_script:"composer-labels" }}
  ```
  where `labels` is built in the view with `gettext`, or render the translated string into a `data-*` attribute. The composer's `cLabel` and the flow builder follow this pattern.
- Don't translate platform names, data from the database, CSS classes, Alpine attributes, URL names.
- After `makemessages`, open every new `#, fuzzy` entry: it was guessed from a similar string ("Insert" became "Inserir emoji", "Reference code" became "Preferências"). Fix the text and remove the flag. Full method and vocabulary: the `translation` skill.

## Comments

`{# #}` is **single-line only**. Spread over two lines it renders as page text (this shipped in the billing staff console). Use `{% comment %}…{% endcomment %}` for anything longer.

## Styling

- Use the tokens and component classes in `theme/static_src/src/styles.css` (`btn-brand`, `btn-*`, `.alert-*`, status pills via `approval_extras.status_badge`). See `docs/style-guide.html`. Colour utilities are `brand-*` / `neutral-*` (token scale, not Tailwind's: primary `brand-700`, border `neutral-300`); `orange-*` / `stone-*` are gone — `docs/brand-and-layout.md` §2.
- Pages render in a centred column on the page surface (`base.html`, `max-w-7xl`): the white goes on cards (`a-card`), never on the page. A full-height screen (inbox, gallery, a canvas) is a card that fills the column (`flex: 1; height: 100%; overflow: hidden`) — never cancel `<main>`'s padding with negative margins. Component classes are in `@layer components`, so a utility on the same element wins.
- Component classes are not in a cascade layer, so Tailwind utilities can't override them (`btn-brand px-4` keeps the component padding). Add a variant instead of fighting it.
- New utility classes need the Tailwind watcher (`npm run start` in `theme/static_src`) or a build to appear.
- Check long text and narrow widths: overflowing columns ("0 / 10 GB0%") and clipped toolbars have shipped before.

## Alpine

- Gestures (drag, connect ports, pan): call `preventDefault` on the pointer event — `.stop` only stops Alpine propagation and the browser still selects text. Don't `preventDefault` when the press lands on an input, textarea, select or button, or the field can't take focus.
- **Every inline `<script>` carries `nonce="{{ request.csp_nonce }}"`.** Production enforces the CSP (development only reports it), so a script without it works locally and is blocked online — one did that to the whole idea board. `apps/common/tests/test_inline_script_nonce.py` fails on any template that forgets it; scripts that htmx swaps in get the page's nonce through the `htmx-config` meta in `base.html`.
- Keep component state in `x-data` on the closest container; large builders (`templates/automations/flow_builder.html`) read server data from `json_script` tags.

## Before finishing

- The post-edit hook checks multi-line comments and `load i18n`; still render the page (runserver or a view test) when the change is more than copy.
- New strings: run `makemessages`, translate, no fuzzy left, `compilemessages`.
