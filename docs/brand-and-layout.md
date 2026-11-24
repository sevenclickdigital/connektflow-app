# Connekt Flow: brand and layout

The product is called **Connekt Flow** (domain `connektflow.com.br`). It is a fork of the open-source BrightBean Studio. This document is the working context for the brand and the layout: what is in the code, the rules it established, and what is left.

The design source is the brand pack in `branding/` (logo, mascot poses and per-network art, references, tokens). `logo/` holds the Connekt Flow logo as the product uses it (`connektflow-wordmark-dark` and `connektflow-symbol` for light backgrounds, `connektflow-wordmark` and `connektflow-symbol-light` for dark ones). The Connekt Flow identity has its own green palette (the mark's accent is `#13E288` in the original artwork, which is not kept in the repository). The product uses the name and the mark, **not that palette**: the interface stays on the violet tokens, and the mark's accent is drawn in the primary violet. Its `README.md` holds the full component anatomy, `tokens/connektflow-tokens.css` the token values, `referencias/referencia_style_guide.png` the official palette and type, and `prova-de-conceito.html` a clickable mock of the new shell, the "Contas Sociais" page and the calendar. When this document and the code disagree, the code is right; when the code and the pack disagree on a component that has not been migrated yet, the pack is the target.

## 1. Naming rules

- User-facing name: **Connekt Flow**, two words, with a K (masculine in Portuguese: "do Connekt Flow", "pelo Connekt Flow"). Identifiers that cannot hold a space use `connektflow` / `ConnektFlow`.
- A workspace is called **marca** in Portuguese (feminine: "a marca", "esta marca", "todas as marcas"). Only the pt_BR catalogue says it; code, URLs, English text and the API keep `workspace`.
- The legal pages (`/privacidade/`, `/termos/`, `/exclusao-de-dados/`) name the product with `APP_NAME` in `apps/common/legal_views.py`, which must equal the display name of the Meta app (today "Connekt Flow", the same as the interface): Meta App Review compares the two. Change them only together.
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

- Emails keep literal hex (mail clients ignore CSS variables): primary `#7C3AED`, text `#0F172A`, border `#E5E7EB`, page `#F5F3FF`. The layout is `templates/emails/base.html`; its logo is `static/img/email/connektflow-wordmark-dark.png`, a PNG on white (mail clients do not show SVG) made from `branding/logo/connektflow-wordmark-dark.png`. Moti waving stands at the right of the header, across its divider (about half above the line, half below it, in a cell that spans the header, divider and spacer rows; the line stops where he starts): `static/img/email/moti-acenando.png`, a transparent PNG 300 px wide shown at 190 px (132 px on a phone), not the WebP poses the site uses, which Outlook does not show.
- `amber-*` stays the warning colour; it is not brand.

## 3. Done

- Rename in templates, Python messages, the `pt_BR` catalogue, API/MCP titles (`SERVER_NAME = "connektflow"`), Asaas charge descriptions, User-Agents, the mail-class header (`X-ConnektFlow-Email-Class`), the web manifest.
- Sign-up and terms-acceptance link to our own `/termos/` and `/privacidade/` instead of the upstream site. The Mastodon app registration advertises `APP_URL` as its website.
- Tokens from the pack in `styles.css`; `brand-green` removed (checkbox/toggle accent is now `--primary`); the undefined `--primary-rgb` fallbacks replaced by `color-mix()` on `--primary`.
- Inter, self-hosted in `static/fonts/` (variable, latin + latin-ext, OFL licence alongside). `--font-display` is Inter too; no serif face is left.
- Logos: `static/img/connektflow-symbol.svg` (auth pages, collapsed sidebar) and `connektflow-wordmark-dark.svg` (sidebar, mobile top bar), identical copies of the files in `branding/logo/`. Favicons and manifest icons are the symbol in white on a violet square, drawn from the symbol's three polygons.
- Shell in `templates/base.html`: sidebar on `--surface-sidebar` with the lockup, the workspace card, the menu, Moti and the profile (name, role, log out). Every page sits in one centred column (`max-w-7xl`); white is on the cards, never on `<main>`. The flow builder (`automations/flow_builder.html`, `flow_new.html`) is the one exception: it empties `{% block main_padding %}` and `{% block content_width %}` to fill the whole area beside the sidebar. Every page scrolls as a whole, header included: `<main>` scrolls, with the scrollbar at the window's edge. The app-like screens whose panels scroll on their own (the inbox, the flow builder, the composer) bound the column to the window instead, setting `{% block column_height %}` to `min-h-0` and `{% block content_scroll %}` to `min-h-0 overflow-y-auto overflow-x-hidden`.
- Menu (`templates/base.html`, `sidebar_nav`): Dashboard (points at `/`, the home redirect, until it has a page); **Publicar** group: Calendário (every Publish mode — calendar, list, board — and the composer; a post starts from a calendar day, so there is no "Agendar post" entry), Gestão de Tarefas, Central de ideias (the idea board, the templates and the special dates); **Engajar** group: Inbox (every channel, WhatsApp included, filtered on the page), Automação, Contatos (`apps/contacts/`; its Funis, Listas, Segmentos and Campanhas tabs are still "Em breve"); Relatórios, Contas Sociais. API keys live only in the account/organization settings ("Chaves de API"). Under "Mais": Galeria, Radar, Notificações, Intelligence. A group opens by itself on one of its pages and otherwise remembers its state (`localStorage`). Menu labels use the `menu` translation context, so they can differ from the same word elsewhere. The channels section is gone, and with it the per-request channel queries in `sidebar_context`.
- Workspace settings (opened from "Settings" on the brand's row in the account centre's Organizations page) swap the main menu for their own (`layouts/workspace_settings.html`): General, Approval flow (`workspaces:approvals_settings`), Client portal, Manage team, API keys (only for someone who manages keys in the organization that owns the brand), then a "Back to organizations" button under the menu (to the brand's calendar for someone who is only a guest of the brand); the brand's row on the Organizations page links to each of them, the approvals shortcut opening the Approval flow page (`workspaces:team`: the brand's members and roles, add from the brand's organization, invite by name and email (`Invitation.name` greets the person in the e-mail and names an account that has no name when it accepts), and the brand's pending invitations with resend and cancel; an invitation for one brand is worded around the brand, `Invitation.team_name`, in the e-mail and on the accept page; `members.services.set_brand_role` enforces the role hierarchy and keeps at least one owner). Billing moved to the account centre: `billing_account:overview` lists every brand whose billing the user manages, and each brand's billing page now opens inside the account centre. The account centre's menu (`layouts/settings.html`) ends with the same kind of button: back to the brand, or to the administrative area for a team account, which reaches that area by signing in and cannot open the product. The account centre (`layouts/settings.html`: profile, preferences, organization general, brands) swaps the sidebar for its own menu and hides the brand switcher and the "+ Novo" button (`{% block sidebar_workspace_switcher %}`, `{% block sidebar_new_button %}`); its Organizations page (`organizations:workspaces`) shows one card per organization the user reaches, with the brands underneath; see `docs/features.md`. The organization calendar and shared library pages have no menu entry any more (brand galleries already show shared assets). The admin area also has its own menu.
- Component classes in `styles.css` live in `@layer components`, so a utility on the same element wins (`btn-brand px-4` means what it says). Buttons are 8 px and size to their content (`.auth-card` buttons stay full width). New classes from the layout: `btn-soft`, `btn-ghost`, `btn-danger-soft`, `a-card`, `a-card-hover`, `page-header`/`page-title`/`page-description`, `section-title`, `a-pill-group`, `chip`, `tile`, `pill` (+ `pill-*`, `is-action`), `modal-*`.
- Social Accounts rebuilt on the layout (see `docs/features.md` F-2.5): problems live in the status pill and its modal, not inside the card.
- Destination picker (pack README, "Seletor de destino"): `.target-picker` / `.target-card` / `.format-toggle`, drawn by `templates/partials/_target_card.html` in the composer's "Where to publish" row and the art request's network picker.
- Full-bleed screens (inbox, gallery, flow builder, message detail) are cards filling the column instead of cancelling the page padding with negative margins; the calendar grid is a card too. Settings forms sit in a card.

## 4. Next steps (in order)

1. **Dashboard** page at `/` (today `/` redirects to the last workspace's calendar), then the Contatos tabs: Funis, Listas, Segmentos, Campanhas.
2. **Page header**: every page fills `{% block page_header %}` with `.page-header` (title, description, primary action on the right). Most pages still put an `h2` inside `content`.
3. **One modal component**: `.modal-*` exists and Social Accounts uses it; the other variants (`rounded-2xl shadow-xl border`, `rounded-xl shadow-xl ring-1`, `.idea-modal`) still need converting.
4. **Calendar** day/post-chip/status classes in `calendar.html` on tokens (the proof of concept has the finished CSS).
5. **Empty states with Moti** (pack §8): poses are converted to WebP in `static/img/moti/` only when a template uses them.
6. **`docs/style-guide.html`**: its classes were codemodded, but the copy and swatches still describe the orange palette; rewrite it against the pack.
7. Radius: many templates still hard-code `rounded-full` buttons and `rounded-xl` cards. Converge as each screen is migrated, not in a sweep.

## 5. Open decisions

- Publicações, Galeria, Radar and Notificações sit under "Mais" until they are placed in a group.
- Whether the sidebar "+ Novo" button stays or the primary action moves to each page header (the pack's mock has no "+ Novo"). It stays for now: it is the only entry to the quick "new idea" modal.
- The Trustpilot review card (`templates/partials/_review_banner.html`) asks for a review of BrightBean on its Trustpilot page and says the product is free. It is off (`REVIEW_BANNER_ENABLED`, default false) until it points at Connekt Flow's own review page.
- Dark mode: not specified by the pack.
- Moti's art is to be revised for the Connekt Flow identity; until the new images arrive the current ones stay.
