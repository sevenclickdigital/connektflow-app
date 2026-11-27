# Aprovações das APIs

Checklist das plataformas que pedem análise (e quase sempre um vídeo) antes de
liberar o app para outros clientes. Marque `[x]` conforme avança. Detalhes de
cada pedido: README (seção da plataforma) e `docs/meta-app-review.md`.

Vídeos: uma pasta por plataforma em `~/Desktop/videos/<plataforma>/`, só com a
versão final de cada permissão (um arquivo por permissão, rascunhos apagados).

Legenda dos passos:
1. API configurada (app criado, chaves no `.env` e no Dokploy)
2. Permissões conferidas e de acordo com as políticas da plataforma
3. Vídeo gravado
4. Enviado para aprovação
5. Aprovado

---

## Unsplash — limite de produção (1.000 buscas/hora)
- [x] API configurada
- [x] Permissões conferidas (só "Acesso público"; crédito ao fotógrafo e download registrado)
- [x] Vídeo gravado (`~/Desktop/videos/unsplash/unsplash-review.mp4`)
- [x] Enviado para aprovação (27/11/2026; o Unsplash leva de 5 a 10 dias úteis, resposta entre 04/12 e 11/12)
- [ ] Aprovado

## Meta — Facebook, Instagram, Messenger e Threads (Advanced Access)
- [ ] API configurada
- [ ] Verificação da empresa (business verification)
- [ ] Permissões conferidas
- [ ] Vídeo gravado (um por permissão, roteiro em `docs/meta-app-review.md`)
- [ ] Enviado para aprovação
- [ ] Aprovado

## Meta — recursos extras (cada um é um pedido separado)
- [ ] Page Public Metadata Access (local no post) — vídeo + envio + aprovado
- [ ] Instagram Public Content Access (hashtags no Radar) — vídeo + envio + aprovado
- [ ] Human Agent (responder DM até 7 dias) — vídeo + envio + aprovado

## Meta Ads
- [ ] API configurada
- [ ] Permissões conferidas (`ads_management`, `ads_read`, `pages_manage_ads`)
- [ ] Vídeo gravado
- [ ] Enviado para aprovação (incluindo Ads Management Standard Access)
- [ ] Aprovado

## WhatsApp (Embedded Signup)
- [ ] API configurada
- [ ] Tech Provider + verificação da empresa
- [ ] Permissões conferidas
- [ ] Vídeo gravado
- [ ] Enviado para aprovação
- [ ] Aprovado

## TikTok
- [ ] API configurada
- [ ] Permissões conferidas (`user.info.basic`, `video.publish`, `video.upload`, `video.list`)
- [ ] Vídeo gravado (no Sandbox do TikTok, mostrando cada permissão)
- [ ] Enviado para aprovação
- [ ] Aprovado

## Google — YouTube
- [ ] API configurada
- [ ] Permissões conferidas (políticas do YouTube, `docs/youtube-compliance-rollout.md`)
- [ ] Vídeo gravado (verificação do OAuth)
- [ ] Enviado para aprovação (verificação do OAuth + auditoria de cota)
- [ ] Aprovado

## Google — Business Profile
- [ ] API configurada
- [ ] Permissões conferidas
- [ ] Vídeo gravado (verificação do OAuth)
- [ ] Enviado para aprovação (pedido de acesso à API)
- [ ] Aprovado

## LinkedIn — perfil pessoal (Path A)
- [x] API configurada
- [x] Permissões conferidas
- [x] Não precisa de vídeo nem de análise — já funciona em produção

## LinkedIn — página da empresa (Path B, Community Management)
- [x] API configurada (segundo app, sem chaves no `.env` ainda)
- [x] Permissões conferidas
- [x] Enviado para aprovação (formulário do nível Development, 25/11/2026)
- [ ] Aprovado (Development) — **aguardando a liberação no Developer Portal**: em 27/11/2026 a aba
  Products ainda mostrava "Review in progress". Até virar "Added", o app não tem os escopos de
  organização e conectar a página cai no "Ocorreu um erro" do próprio LinkedIn. Quando liberar:
  conferir os escopos na aba Auth, pôr `PLATFORM_LINKEDIN_COMPANY_CLIENT_ID/SECRET` no `.env` e
  no Dokploy e testar a conexão da página.
- [ ] Vídeo gravado (para o nível Standard; só dá para gravar depois do Development aprovado)
- [ ] Enviado para aprovação (Standard)
- [ ] Aprovado (Standard)

## Pinterest (publicação e Pinterest Ads)
- [x] API configurada — trial aprovado (26/11/2026), chaves no `.env` e no Dokploy com
  `PINTEREST_API_BASE` no Sandbox, as 4 redirect URIs cadastradas (publicação e Ads, produção e localhost)
- [ ] Permissões conferidas
- [ ] Vídeo gravado (para o acesso Standard)
- [ ] Enviado para aprovação (Standard)
- [ ] Aprovado

## X (Twitter)
- [x] API configurada (plano pago da API)
- [x] Permissões conferidas
- [x] Vídeo gravado — não precisa
- [x] Enviado para aprovação — não precisa
- [x] Aprovado — não passa por análise

## Bluesky
- [x] API configurada
- [x] Permissões conferidas
- [x] Vídeo gravado — não precisa
- [x] Enviado para aprovação — não precisa
- [x] Aprovado — não passa por análise

## Mastodon
- [x] API configurada
- [x] Permissões conferidas
- [x] Vídeo gravado — não precisa
- [x] Enviado para aprovação — não precisa
- [x] Aprovado — não passa por análise

## DEV.to
- [x] API configurada
- [x] Permissões conferidas
- [x] Vídeo gravado — não precisa
- [x] Enviado para aprovação — não precisa
- [x] Aprovado — não passa por análise
