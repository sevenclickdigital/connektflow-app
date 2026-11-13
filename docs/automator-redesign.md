# Automator: rename and new layout

The product is called **Automator** (it was BrightBean Studio, the open-source project this repository forks). This document is the working context for the rename and for the new layout: what is already in the code, the rules it established, and what is left.

The design source is the brand pack in `NOVO-LAYOUT/Automator_Brand_Assets/` (not committed; 31 MB of artwork). Its `README.md` holds the full component anatomy, `tokens/automator-tokens.css` the token values, `referencias/referencia_style_guide.png` the official palette and type, and `prova-de-conceito.html` a clickable mock of the new shell, the "Contas Sociais" page and the calendar. When this document and the code disagree, the code is right; when the code and the pack disagree on a component that has not been migrated yet, the pack is the target.

## 1. Naming rules

- User-facing name: **Automator** (masculine in Portuguese: "do Automator", "pelo Automator").
- The Meta-facing legal name stays **Seven Automator** (`apps/common/legal_views.py`), because Meta App Review compares it with the app's configuration.
- Kept on purpose, because changing them breaks running installations or third-party contracts:
  - HKDF `info` strings in `apps/common/encryption.py` and `apps/api_keys/services.py` (changing them makes every stored secret and API key unreadable);
  - database names `brightbean` / `brightbean_test` (compose files, CI, settings);
  - the Mastodon `idempotency_key` prefix in `apps/publisher/engine.py` (a retry after deploy would get a new key and could double-post);
  - the public API field `calculated_by_brightbean` (`apps/api/schemas.py`);
  - migration `accounts/0004_set_site_brightbean.py`;
  - licence attributions to `brightbean-chat` in `apps/automations/`, and the "fork of BrightBean Studio" statements in docs;
  - everything about the optional **BrightBean Intelligence** SaaS (`apps/intelligence/`), including `support@brightbean.xyz`: it is a third-party service run by upstream.

## 2. Colours: how templates name them

`theme/static_src/src/styles.css` holds the token `:root` from the pack and exposes it to Tailwind through `@theme inline`:

- `brand-50 … brand-900` → `var(--brand-*)`. The primary is **`brand-700`** (`#7C3AED`), hover `brand-800`, soft background `brand-100`/`brand-50`.
- `neutral-50 … neutral-950` → `var(--neutral-*)`. The scale is the pack's, not Tailwind's stock one, so the numbers mean: border `neutral-300`, hover border `neutral-400`, tertiary text/icons `neutral-500`, secondary text `neutral-600`, body text `neutral-900`/`neutral-950`, lilac surfaces `neutral-50`–`neutral-200`.
- Do not write `orange-*` or `stone-*` classes, or the old hex values (`#F97316`, `#1C1917`, …). They were codemodded out with this mapping, kept here for reading old diffs:

  | old | new | | old | new |
  |---|---|---|---|---|
  | `orange-50…300` | `brand-50…300` | | `stone-50`, `stone-100` | `neutral-50`, `neutral-100` |
  | `orange-400` | `brand-500` | | `stone-200` | `neutral-300` |
  | `orange-500` | `brand-700` | | `stone-300…800` | `neutral-400…900` |
  | `orange-600` | `brand-800` | | `stone-900` | `neutral-950` |
  | `orange-700` | `brand-900` | | | |

- Emails keep literal hex (mail clients ignore CSS variables): primary `#7C3AED`, text `#0F172A`, border `#E5E7EB`.
- `amber-*` stays the warning colour; it is not brand.

## 3. Done

- Rename in templates, Python messages, the `pt_BR` catalogue, API/MCP titles (`SERVER_NAME = "automator"`), Asaas charge descriptions, User-Agents, the mail-class header (`X-Automator-Email-Class`), the web manifest.
- Sign-up and terms-acceptance link to our own `/terms/` and `/privacy/` instead of the upstream site. The Mastodon app registration advertises `APP_URL` as its website.
- Tokens from the pack in `styles.css`; `brand-green` removed (checkbox/toggle accent is now `--primary`); the undefined `--primary-rgb` fallbacks replaced by `color-mix()` on `--primary`.
- Inter, self-hosted in `static/fonts/` (variable, latin + latin-ext, OFL licence alongside). `--font-display` is Inter too; no serif face is left.
- Logos: `static/img/automator-symbol.webp` (auth pages, collapsed sidebar) and `automator-wordmark-dark.webp` (sidebar, mobile top bar). Favicons and manifest icons regenerated from the symbol.
- Shell in `templates/base.html`: sidebar on `--surface-sidebar` with a right border and the lockup on top; `<main>` without the white bordered container, content sits on `--surface-page`.

## 4. Next steps (in order)

1. **Navigation** (pack §6 and §10): reorder to Dashboard, Planejamento, Publicações, Calendário, Conteúdo (IA), Relatórios, Contas Sociais, Integrações, Configurações; remove the channels section; mascot `moti_trabalhando_no_computador` above the profile. Needs the open decisions below.
2. **Page header**: every page fills `{% block page_header %}` with title 24/600, description, primary action on the right. Pages that put the title inside `content` (e.g. `social_accounts/list.html`) move it up.
3. **One modal component** (`.modal-overlay > .modal-panel …`, pack §6) replacing the three current variants (`rounded-2xl shadow-xl border`, `rounded-xl shadow-xl ring-1`, `.idea-modal`).
4. **Contas Sociais**: platform card and compact connection card; problems move from inline `bg-amber-50`/`bg-red-50` blocks in `_account_card.html` to an actionable status pill that opens the modal.
5. **Calendar**: toolbar and grid inside a card, day/post-chip/status classes on tokens (`calendar.html`; the proof of concept has the finished CSS).
6. **Empty states with Moti** (pack §8): convert the needed poses to WebP (max 480 px tall) in `static/img/moti/` only when a template uses them.
7. **`docs/style-guide.html`**: its classes were codemodded, but the copy and swatches still describe the orange palette; rewrite it against the pack.
8. Radius: the pack makes controls 8 px and cards 12 px; many templates still hard-code `rounded-full` buttons and `rounded-xl` cards. Converge as each screen is migrated, not in a sweep.

## 5. Open decisions

- Where Inbox and Galeria go in the new menu (suggestion: Inbox after Publicações, Galeria inside Conteúdo).
- Whether the sidebar "+ Novo" button stays or the primary action moves to each page header (the pack's mock has no "+ Novo").
- The Trustpilot review card (`templates/partials/_review_banner.html`) asks for a review of BrightBean on its Trustpilot page and says the product is free. Remove it, or point it at Automator's own review page.
- Dark mode: not specified by the pack.
- `README.md` is still upstream's (logo, badges, hosted version at brightbean.xyz). Rewrite it for Automator, keeping the AGPL notice and the credit to BrightBean Studio.
