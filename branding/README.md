# Automator — Pacote de identidade visual

Fonte de verdade para o novo layout do painel. Tudo o que for aplicado no código sai daqui; quando uma imagem de referência e este README divergirem, vale a ordem de precedência da seção 2.

## 1. Estrutura

| Caminho | Conteúdo | Dimensões |
|---|---|---|
| `logo/automator-symbol.svg` / `.png` | Só o símbolo "A" em gradiente roxo, fundo transparente | 512×512 / 1024×1024 |
| `logo/automator-wordmark.svg` / `.png` | Lockup horizontal (símbolo + "Automator"), texto **branco** — só para fundo escuro ou roxo | 1200×320 / 1800×480 |
| `logo/automator-wordmark-dark.svg` / `.png` | Mesmo lockup, texto `#0F172A` — para fundo claro (sidebar, login, e-mails) | 1200×320 / 1800×480 |
| `logo/fonte/automator-wordmark-original.svg` | Arquivo original do lockup branco, em resolução maior. Só para gerar novas exportações | 2172×724 |
| `mascote/poses/` | Poses do Moti (`moti_pose_*`), PNG com alpha, sem fundo | 623–1754 px de largura |
| `mascote/ilustracoes/` | 2 cenas do Moti (calendário, laptop), **também** PNG com alpha, sem fundo | 1536×1024 |
| `mascote/redes/` | Moti segurando o logo de cada rede (`moti_rede_<plataforma>`, com a chave da plataforma no código: `instagram`, `google_business`…), PNG com alpha | 1536×1024 |
| `referencias/referencia_style_guide.png` | **Base oficial**: paleta, tokens, tipografia e componentes | 1536×1024 |
| `referencias/referencia_dashboard_contas_sociais.png` | **Base oficial** de componentes: mock da tela "Contas Sociais" (ver pendências — ainda não está no diretório) | — |
| `referencias/preview_identidade_automator.png`, `preview_pacote_marca.png` | Pranchas de conceito: use só para mascote e variações de logo. **A paleta delas (`#5C22C4`) está superada** | 1536×1024 |
| `referencias/conceito_inicial_marca_mascote.png` | Arte de conceito (Moti com capa, ícones das redes). Só inspiração | 1536×1024 |
| `referencias/banner_divulgacao_automator.png` | Banner de divulgação (Moti, logo, slogan e recursos) sobre fundo roxo escuro. Só para material de marketing | 1536×1024 |
| `tokens/automator-tokens.css` | Bloco `:root` pronto para substituir o de `theme/static_src/src/styles.css` | — |
| `prova-de-conceito.html` | Validação visual com os tokens linkados: shell novo (sidebar lilás, conteúdo sem container branco) e três páginas alternáveis pela própria navegação — "Contas Sociais", "Calendário" (tela Publicar) e "Guia de estilo" (componentes + mapa de migração). Abrir direto no browser (usa Tailwind e Alpine por CDN; precisa de internet). Mesma marcação das templates | — |

Os SVGs da logo embutem a arte em bitmap (a origem já era assim). Para impressão ou ampliações grandes, exporte um vetor real da ferramenta de design.

## 2. Precedência

1. `tokens/automator-tokens.css` — valores finais.
2. `referencias/referencia_style_guide.png` — paleta, tipografia, raio, sombra.
3. Mock "Contas Sociais" — anatomia dos componentes (seção 6).
4. Demais pranchas — só mascote e logo.

## 3. Cores

Primária `#7C3AED` (brand-700), hover `#6D28D9`, soft `#F3E8FF`, ring `#C4B5FD`. A escala completa (50–900), os neutros, as semânticas e os destaques estão em `tokens/automator-tokens.css` com os mesmos nomes que o `styles.css` já usa — não há renomeação, só troca de valor.

Regras:
- Roxo só em ação e estado ativo: botão primário, aba/chip ativo, link, foco, ícone de destaque. Fundo de página e cards ficam neutros.
- Fundo de página `#F8FAFF`; sidebar `#F5F3FF` (`--surface-sidebar`); card `#FFFFFF` com borda `#E5E7EB` 1px e raio 12px; cabeçalho de card/tabela `#FAFAFF`.
- Texto: `#0F172A` principal, `#64748B` secundário, `#94A3B8` auxiliar.
- Estados: sucesso `#22C55E`/`#ECFDF5`, erro `#EF4444`/`#FEF2F2`, atenção `#F59E0B`/`#FFFBEB`, info `#3B82F6`/`#EFF6FF`. Pill = fundo soft + bolinha 500 + texto 700.
- Destaques (`sky`, `teal`, `rose`, `indigo`, `amber`) são para gráficos, tags de categoria e tiles de ícone — nunca para botões.
- Sombra padrão de card: `0 1px 3px rgba(0,0,0,.06), 0 6px 20px rgba(124,58,237,.08)` (`--shadow-md`). Sem sombras cinza-neutras.

## 4. Tipografia

Inter em tudo (corpo e títulos). Não existe fonte serifada de display.

| Elemento | Peso | Tamanho | Token |
|---|---|---|---|
| Título da página | 600 | 24px | `--text-page-title` |
| Título de seção | 600 | 18px | `--text-section-title` |
| Título de card | 500 | 16px | `--text-card-title` |
| Texto normal | 400 | 14px | `--text-body` |
| Texto auxiliar | 400 | 12px | `--text-aux` |
| Rótulo / tag / cabeçalho de tabela | 500 | 12px | `--text-label` |

Pesos necessários: 400, 500, 600 (700 opcional para números grandes dos stat cards).

## 5. Raio, borda e espaçamento

- Controles (botão, input, select, chip): 8px (`--radius-md`).
- Cards, dropdowns, modais: 12px (`--radius-lg`).
- Pills de status, abas de filtro, avatares: full.
- Borda sempre 1px `#E5E7EB`; hover `#CBD5E1`.
- Grade de cards com gap 16px; padding interno de card 16–20px.

## 6. Componentes (anatomia dos mocks "Contas Sociais" e "Publicar")

- **Shell**: sidebar 240px (60px recolhida) com fundo `--surface-sidebar` e borda direita `--border`; conteúdo direto sobre `--surface-page`, **sem** o container branco com borda e raio que `base.html` põe hoje no `<main>` (`lg:border lg:m-2.5 bg-white lg:rounded-xl`). O que é branco são os cards.
- **Sidebar, de cima para baixo**: logo (`automator-wordmark-dark`, 32px de altura); seletor de workspace; navegação; mascote (`moti_trabalhando_no_computador`) e, no rodapé, o perfil (avatar + nome + papel). Não há seção de canais: os canais aparecem no filtro "Canais" do calendário e em Contas Sociais.
- **Navegação** (ordem fixa): Dashboard, Planejamento, Publicações, Calendário, Conteúdo (IA), Relatórios, Contas Sociais, Integrações, Configurações. Item 40px, ícone 18px traço 2, texto 14/500; ativo = fundo `--primary-soft` + texto `--primary` 600; hover `rgba(124,58,237,.06)`.
- **Cabeçalho de página** (todas as telas, `{% block page_header %}` de `base.html`): à esquerda título 24/600 + descrição 14/400 secundária; à direita a ação primária da tela (`.btn-brand` com `+`) e, quando houver, controles secundários (ex.: alternador Lista/Calendário/Quadro). Exemplos: Contas Sociais → "Conectar nova plataforma"; Publicar → "Nova publicação"; Planejamento → "Nova tarefa"; Relatórios → "Exportar".
- **Botão primário**: fundo `--primary`, texto branco, 8px, ícone à esquerda. Hover `--primary-hover`.
- **Botão secundário / soft**: fundo `--primary-soft`, texto `--primary`, sem borda (ex.: "Conectar", "Reconectar").
- **Botão outline**: fundo branco ou `--brand-50`, borda `--primary-ring`, texto `--primary`, largura total no rodapé de card (ex.: "Gerenciar conexão" com ícone de engrenagem).
- **Botão fantasma**: fundo `--surface-1`, texto `--text-primary`, borda `--border`.
- **Botão excluir**: fundo `--error-50`, texto `--error-500`, borda `--error-50`.
- **Stat card**: branco, borda, 12px; tile de ícone 40×40 com fundo soft colorido (sucesso, roxo, erro, roxo) e ícone na cor 500; número 24/700 ao lado + rótulo 14 secundário.
- **Abas de filtro**: contêiner `--surface-2` em pill; aba ativa pill `--primary` texto branco; inativas texto `--text-secondary`. Contagem entre parênteses.
- **Chips de categoria**: ativo igual à aba ativa; inativos brancos com borda e ícone à esquerda.
- **Busca e select**: brancos, borda, 8px, altura 40px; ícone de lupa à esquerda; chevron à direita.
- **Cabeçalho de seção**: ícone 20px em cor semântica/roxa + título 18/600.
- **Card de plataforma**: ícone da rede 40×40 (cor oficial da rede) + nome 16/500 + descrição 12 secundária; pill de status à direita; divisor; linha da conta com avatar 32, nome 14/600, @handle 12 secundário, menu kebab; botão outline largura total.
- **Card de conexão (compacto)**: ícone 40 + nome 14/600 + descrição 12; botão soft "Conectar" à direita. Recurso pago leva ícone de coroa no canto.
- **Pill de status**: Conectada (sucesso), Com problema (erro), Atenção (atenção/âmbar), Disponível (roxo). Texto 12/500.
- **Problemas e avisos** (regra para todo card ou item de lista): nunca inline dentro do card. O pill de status carrega o estado — âmbar para aviso (permissão faltando, token expirando, webhook sem assinatura), vermelho para erro (token expirado, conta desconectada pela rede) — e vira botão (`.pill.is-action`, com seta). O clique abre um modal: ícone no tom do estado, título, conta afetada, uma explicação em linguagem de usuário e as ações (Reconectar como primária, Desconectar como `.btn-danger-soft`, Fechar como `.btn-ghost`). O rodapé do card não muda com o estado ("Gerenciar conexão" sempre). No código isso substitui os blocos `bg-amber-50`/`bg-red-50` de `_account_card.html` e os botões Reconectar/Desconectar que hoje aparecem no rodapé do card.
- **Ícones**: traço 1.5–2px, 20px em botões e cabeçalhos, 24px em tiles. Reaproveitar o conjunto de ícones que o projeto já usa; não há ícones no pacote.
- **Seletor de destino** (compositor "Onde publicar" e seletor de redes do pedido de arte): substitui os círculos `.target-tile` (conta × formato, avatar igual em todos, nome só no tooltip, plataformas não conectadas apagadas na mesma fileira). Um `.target-card` de 240px por conta conectada: avatar 28px, nome, rede + handle com o ícone colorido da rede, check quando há algo escolhido; os formatos (Feed, Story, Reel) são `.format-toggle` dentro do card (`.is-on` = primário; `.is-blocked` = a mídia anexada não serve, com o motivo no `title`). Card com seleção = borda `--primary` + fundo `--brand-50`. Conta com um só formato tem um toggle só; no pedido de arte (sem formatos) o card inteiro é o toggle. As plataformas não conectadas viram **um** card tracejado "Conectar canal" com a contagem. Acima da fileira: rótulo "Onde publicar", contagem de destinos e "Limpar". Em telas estreitas a fileira rola na horizontal. `composerApp()` não muda (`toggleFormat` / `hasFormat`); `partials/_channel_tile.html` vira `_target_card.html` com os mesmos argumentos.
- **Modal** (um só modelo para toda caixa de diálogo): `.modal-overlay > .modal-panel > .modal-close + .modal-header + .modal-body + .modal-footer`. Painel 448px (`.is-sm` 384 para confirmações, `.is-lg` 640 para conteúdo), raio `--radius-xl`, borda, `--shadow-xl`, padding 24px; fundo `rgba(15,23,42,.35)` com blur 2px. Fecha por Esc, clique no fundo e pelo "x" — exceto confirmações destrutivas, que só fecham pelos botões. Header: ícone 44px redondo no tom do caso (`.modal-icon.is-brand/success/warning/error/info`) + título 16/600 + meta 12 opcional (conta, canal, data); centralizado (`.is-centered`) só em confirmações. Rodapé alinhado à direita, na ordem Fechar/Cancelar (`.btn-ghost`) → destrutivo (`.btn-danger-soft`) → primário (`.btn-brand`); em confirmações, dois botões de largura igual (`.is-split`). Entrada 150ms (opacidade + escala 95%), saída 100ms. Alpine controla abrir/fechar (`x-show` + `x-transition`), como os modais já existentes em `base.html`. No código, substitui as três variações atuais (`rounded-2xl shadow-xl border`, `rounded-xl shadow-xl ring-1`, `.idea-modal`).
- **Calendário (Publicar)**: toolbar e grade dentro de um card branco (`.a-card`, sem padding). Toolbar: setas 28px, período 16/700, "Hoje" e "Mês ▾" como `.cal-filter-select`; à direita o toggle "🎉 Datas comemorativas" (âmbar quando ativo) e os `ui_select` de status, canais, tags e fuso. Grade: cabeçalho dos dias em `--surface-1`, 12/600 `--text-tertiary`; célula mínima 130px com borda `--border`; dia passado `--surface-1`; hoje `--brand-50` com o número em círculo `--primary`; outro mês a 40%. Feriado: emoji ao lado do número + nome 9px `--warning-700`. Chip de post (`.post-chip--compact`): tile 14px com o ícone da rede, título truncado, hora em tabular; cor pelo status, mesma paleta dos pills. Botão `+` `--primary` aparece no hover da célula. Legenda de status alinhada à direita, abaixo da grade.

## 7. Uso da logo

- Sidebar e cabeçalho sobre fundo claro: `automator-wordmark-dark`. Sidebar recolhida e favicon: `automator-symbol`.
- Login/cadastro/aceite de termos (hoje usam `static/img/brightbean-studio-logo.webp` em 48×48): `automator-symbol`.
- Fundo roxo ou escuro (e-mail com header roxo, splash, OG image): `automator-wordmark` (texto branco).
- Altura mínima do lockup 24px; do símbolo 16px. Área de respiro = altura do símbolo ÷ 4.
- Não recolorir o símbolo, não aplicar sombra, não esticar, não girar. Não usar o lockup branco sobre fundo claro.

## 8. Uso do mascote (Moti)

| Pose | Arquivo | Onde usar |
|---|---|---|
| Pensativo / dúvida | `moti_pose_pensativo_interrogacao_corpo_inteiro`, `moti_pose_duvida_interrogacao_recorte` | Estado vazio de busca, 404, "nenhum resultado" |
| Joinha / aprovação | `moti_pose_joinha_corpo_inteiro` | Confirmação, aprovação concluída, sucesso em onboarding |
| Comemorando | `moti_pose_comemorando_corpo_inteiro`, `moti_pose_comemorando_recorte` | Primeira publicação, meta batida, fim de onboarding |
| Braços cruzados (base) | `moti_pose_base_bracos_cruzados` | Páginas de ajuda, "sobre", avatar do assistente |
| Com calendário | `ilustracoes/moti_com_calendario` | Estado vazio do calendário / agendamentos |
| No computador | `ilustracoes/moti_trabalhando_no_computador` | Estado vazio do compositor, "processando", worker |
| Buscando com lupa (capa) | `moti_pose_capa_buscando_lupa` | Busca em andamento, Radar, pesquisa de perfis |
| Sem resultados (lupa) | `moti_pose_sem_resultados_lupa` | Busca sem resultado, galeria de mídia vazia |
| Conexão perdida (capa) | `moti_pose_capa_conexao_perdida`, `moti_pose_capa_conexao_perdida_paisagem` | Conta desconectada, token expirado, falha de integração |
| Braços cruzados (capa) | `moti_pose_capa_bracos_cruzados` | Herói de páginas de marketing, onboarding |
| Com o logo da rede | `redes/moti_rede_<plataforma>` | Card de conectar a rede, onboarding de cada canal, estado vazio filtrado por rede |

Regras: tamanho máximo 240px de altura em estados vazios, 120px em cards; sempre com `alt` traduzido; nunca esticar; sempre sobre fundo claro ou `--brand-50`. Os "recortes" (busto) servem para espaços baixos; os "corpo inteiro" para colunas.

## 9. Como aplicar no código

1. Substituir o bloco `:root` de `theme/static_src/src/styles.css` pelo de `tokens/automator-tokens.css`. Os nomes são os mesmos; só mudam valores. Remover `--brand-green-*` (sobra da marca antiga) depois de conferir onde é usado.
2. `base.html`: fundo `--surface-sidebar` na `<aside>` (hoje `lg:bg-transparent`); tirar `lg:border lg:border-stone-200 lg:m-2.5 bg-white lg:rounded-xl` do `<main>`; reordenar a navegação conforme a seção 6 e remover a seção de canais. Decidir o destino dos itens que não existem no menu novo (seção 10).
3. Toda tela preenche `{% block page_header %}` com título + descrição + ação (seção 6). As que hoje põem o título dentro de `{% block content %}` (ex.: `social_accounts/list.html`) sobem para o bloco.
4. Carregar a Inter (400/500/600) — hoje o projeto não carrega nenhuma webfont. Recomendado: self-host em `static/fonts/` com `@font-face` no `styles.css` e `font-display: swap`, sem chamada externa.
5. Revisar as classes que apontam para `--font-display` (era serifada): agora é Inter, o peso 600 faz o papel de destaque.
6. Copiar `logo/automator-symbol.png` e `logo/automator-wordmark-dark.png` (ou os SVGs) para `static/img/` e trocar as 4 referências a `brightbean-studio-logo.webp` (`templates/account/login.html`, `signup.html`, `accept_terms.html`, `templates/oauth2_provider/authorize.html`) e a sidebar. Gerar favicon 16/32/48 e `apple-touch-icon` 180 a partir do símbolo.
7. `calendar.html`: trocar os literais `#F97316`/stone das classes `.cal-day`, `.post-chip`, `.status-*`, `.view-btn`, `.cal-add-btn` pelos tokens (a prova de conceito tem a versão pronta).
8. Atualizar `docs/style-guide.html` com a nova paleta e os componentes da seção 6; ele é a referência do projeto e precisa bater com o `styles.css`.
9. Rodar `npm run build` em `theme/static_src` e conferir no browser (runserver 8003) as telas: login, sidebar, Contas Sociais, calendário, compositor, aprovações, inbox, billing.

## 10. Pendências e decisões em aberto

- **Mocks não estão no diretório.** Salvar "Contas Sociais" como `referencias/referencia_dashboard_contas_sociais.png`, o print do menu como `referencias/referencia_sidebar.png` e o calendário atual como `referencias/calendario_atual.png`.
- **Mapa do menu antigo → novo**: Publish → Calendário (modo calendário) e Publicações (modo lista/fila); Tarefas + Criar ideia → Planejamento; Radar + compositor → Conteúdo (IA); Analytics → Relatórios; Automações + API/MCP → Integrações. **Inbox social e Galeria não têm lugar no menu novo** — decidir se entram (sugestão: Inbox depois de Publicações; Galeria dentro de Conteúdo) ou viram abas internas.
- **Botão "+ Novo" da sidebar**: o mock não o tem; a prova move a ação primária para o cabeçalho de cada página. Confirmar.
- **Inter**: self-host (recomendado) ou Google Fonts. Afeta privacidade (LGPD) e CSP.
- **Modo escuro**: não especificado. O lockup branco existe, mas não há tokens dark.
- **Ícones**: as pranchas prometem um conjunto `/icones`; ele não veio. Usar o conjunto já presente no projeto.
- **Lockup vertical** (símbolo sobre o nome) aparece nas pranchas, mas não foi entregue. Gerar só se alguma tela precisar.
- **Pill "Atenção"**: o mock usa tom rosado; o style guide define âmbar. Seguir o style guide.
- **Neutros**: a escala mistura lilás (50–200) com slate (300–900). É intencional no style guide — fundos levemente lilás, textos e bordas cinza-azulados.
