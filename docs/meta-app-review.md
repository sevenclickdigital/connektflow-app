# App Review da Meta: o que enviar, permissão por permissão

Para cada permissão e recurso que o app da Meta pede: o que o código faz com ela, o texto para colar no formulário do App Review e o roteiro da gravação de tela.

- **App:** Connekt Flow, App ID `4547728542177947`, portfólio empresarial `1575031336120607`.
- **Verificado em:** 18 de novembro de 2026, contra o código em `main` e o estado do app lido pelas ferramentas de desenvolvedor da Meta.
- **Escopo:** tudo o que faz login pela Meta: Páginas do Facebook, Instagram (Login do Facebook), Threads, WhatsApp e Meta Ads.

Os blocos "Texto para enviar" estão em inglês porque os revisores da Meta leem em inglês; todo o resto está em português. Os nomes dos botões aparecem como a interface em inglês mostra, com o rótulo em português entre parênteses, porque a Meta recomenda gravar com a interface em inglês.

## 1. Situação do app

| Item | Estado |
|---|---|
| Modo do app | Desenvolvimento. Manter assim até a aprovação: em modo Ao vivo, uma permissão não aprovada para de funcionar até para quem tem papel no app |
| Envio de App Review | Existe um rascunho (`4547728542177947` → App Review), não enviado, com 26 itens |
| Acesso avançado | Nenhum concedido ainda |
| Verificação da empresa | Aprovada |
| Política de privacidade / termos | `https://connektflow.com.br/privacidade/`, `https://connektflow.com.br/termos/` |
| Conformidade | Sem violações nem ações obrigatórias em aberto |
| Webhooks | Página: `feed`, `mention`, `messages`. Instagram: `comments`, `mentions`, `messages`. Conta do WhatsApp Business: `messages`, `account_update`. Todos batem com o código |
| URIs de redirecionamento OAuth | Callbacks do Facebook, Instagram e Meta Ads registrados. O do Threads fica no painel do caso de uso do Threads e não pôde ser lido daqui |

## 2. Corrigir antes de enviar

1. **Nenhuma chamada de teste está registrada.** A Meta mantém o botão "Solicitar acesso avançado" desabilitado até registrar uma chamada de API bem-sucedida por permissão, feita nos 30 dias anteriores ao envio. Todas as permissões mostram 0 de 1. Executar cada roteiro da seção 5 com uma conta que tenha papel no app faz essas chamadas; a Meta leva até 2 dias para contá-las.
2. **`whatsapp_business_management` mostra erro no teste de API**, em vez de "não iniciado". Abra a permissão no painel para ler o motivo antes de gravar.
3. **O Marketing API Access Tier exige 500 chamadas à API de Marketing nos últimos 15 dias, com menos de 15% de erro**, e não uma chamada. Veja a seção 5.5.
4. **A exclusão de dados do usuário não está configurada no app.** Em Configurações do app → Básico → Exclusão de dados do usuário, escolha *URL de retorno de chamada de exclusão de dados* e informe `https://app.connektflow.com.br/data-deletion/`.
5. **O e-mail de contato `contato@connektflow.com.br` não está verificado.** As notificações da análise vão para ele.
6. **`instagram_manage_contents` está no rascunho, mas desligada em produção** (`PLATFORM_INSTAGRAM_FACEBOOK_DELETE_SCOPE_ENABLED=false`). Ou ligue, reconecte a conta do Instagram e grave a exclusão, ou remova a permissão do rascunho. Como está, o diálogo de login da gravação não vai mostrá-la e a Meta rejeita.
7. **Três recursos que o código usa não estão no rascunho**: Instagram Public Content Access, Page Public Metadata Access e Human Agent. Veja a seção 6.
8. **Exigências de gravação da Meta que o app não cobre hoje.** Cada uma está detalhada na sua seção:
   - `pages_manage_posts`: a Meta pede criar, editar e excluir. O app não edita um post que já está no Facebook.
   - `threads_basic`: a Meta pede listar os posts do próprio usuário no Threads. O app mostra apenas os posts publicados por ele.
   - `threads_manage_insights`: a Meta pede métricas do perfil e métricas por post. O provedor lê apenas métricas por post.
   - `whatsapp_business_management`: a Meta pede ver a criação de um modelo de mensagem. O app não cria modelos; o WhatsApp Manager é aceito para isso.
9. **"Ver em Facebook / Instagram / Threads", na gaveta do post em Relatórios, é um link morto** (`href="#"`). A Meta quer ver o resultado no app dela, então abra a Página ou o perfil em outra aba durante a gravação.
10. **Remover do rascunho o que o app não usa**: `email`, os itens de jogos e todas as permissões `instagram_business_*`. O Login do Instagram não está configurado em produção. Selecionar permissões desnecessárias é motivo comum de rejeição.

## 3. Regras para todas as gravações

Exigências da Meta, do guia de envio:

- Uma gravação por permissão, mostrando a pessoa concedendo a permissão no diálogo de login e, em seguida, o app usando-a. Permissão sem gravação não é aprovada.
- Interface em inglês. Troque pelo seletor de idioma da barra lateral antes de gravar. Onde a tela não for autoexplicativa, adicione legendas.
- 1080p ou mais, apenas a janela do navegador, largura de tela de 1440 ou menos, cursor do mouse grande, mouse em vez de atalhos de teclado, sem áudio.
- Nas permissões de mensagens, grave a mensagem saindo do app e chegando no Messenger, Instagram ou WhatsApp. Capturas de tela da mensagem recebida são recusadas.

Como preparar:

- Grave em `https://app.connektflow.com.br`, com uma conta do Facebook que tenha papel no app.
- Use uma Página de teste vinculada a uma conta profissional do Instagram, um perfil do Threads adicionado como testador do Threads, um número de WhatsApp e uma conta de anúncios.
- Antes de cada gravação, desconecte a conta no Connekt Flow e remova o Connekt Flow em Facebook → Configurações → Apps e sites, para o diálogo de login mostrar de novo a lista completa de permissões.
- Tenha a conta de uma segunda pessoa pronta para comentar e enviar mensagens como cliente.

### A sequência de login que abre toda gravação de Facebook e Instagram

1. Entre em `https://app.connektflow.com.br/accounts/login/`.
2. Barra lateral → **Social Accounts** (Contas sociais).
3. No card do **Facebook** (ou **Instagram**), clique em **Connect** (Conectar) e depois em **Continue** (Continuar) na janela do guia.
4. Na janela do Facebook, faça login, pare na tela que lista as permissões e confirme.
5. Em **Select Accounts** (Selecionar contas), marque a Página (ou a conta do Instagram) e clique em **Connect Selected** (Conectar selecionadas).
6. Mostre o card da conta com o selo **Connected** (Conectada), o nome, o usuário e o número de seguidores.

As seções abaixo chamam isso de "a sequência de login".

## 4. Instruções para o revisor (campo "como testar")

Texto para enviar:

```
Connekt Flow is a social media management platform for businesses and agencies.
A business connects its own Facebook Pages, Instagram professional accounts,
Threads profiles, WhatsApp numbers and Meta ad accounts, and then schedules and
publishes posts, answers comments and messages in one inbox, reads performance
reports and manages ad campaigns.

There is no "Log in with Facebook". Users sign in to Connekt Flow with email
and password; Meta login is used only to connect business assets.

1. Go to https://app.connektflow.com.br/accounts/login/
2. Sign in with: <reviewer email> / <reviewer password>
3. Switch the interface to English with the language picker in the sidebar.
4. Open "Social Accounts" in the sidebar. Click "Connect" on Facebook,
   Instagram, Threads, WhatsApp or Meta Ads to grant the permissions.
5. Each permission's notes say which screen uses it.

Privacy policy: https://connektflow.com.br/privacidade/
Data deletion instructions: https://app.connektflow.com.br/data-deletion/
```

Crie um workspace e um login dedicados para o revisor e preencha os dois campos entre `< >`.

## 5. Permissões

Cada item traz as chamadas que o código faz, o texto para enviar e o roteiro da gravação. Os caminhos são relativos a `https://graph.facebook.com/v25.0`, salvo indicação em contrário.

### 5.1 Páginas do Facebook

#### `public_profile`

- **Para que serve:** identificar a pessoa que faz login, para nomear a conexão (`GET /me?fields=id,name,picture`).
- **Gravação:** não é exigida. A Meta concede automaticamente; valem apenas as perguntas de tratamento de dados.
- **Texto para enviar:**

```
Connekt Flow reads the id and name of the person who signs in with Facebook to
record who connected a Page or ad account to a workspace. The data is shown only
to that workspace's members and is never used for marketing.
```

#### `pages_show_list`

- **Para que serve:** o seletor de contas depois do login (`GET /me/accounts`, `providers/meta_accounts.py`) e a lista de Páginas de uma conta de anúncios (`GET /{ad-account}/promote_pages`).
- **Texto para enviar:**

```
After Facebook login, Connekt Flow lists the Pages the person manages so they can
choose which ones to connect to their workspace. We show each Page's name,
picture, category and follower count on the "Select Accounts" screen, and only
the Pages the person ticks are connected. This permission is also a dependency
of pages_read_engagement, pages_manage_posts, pages_manage_engagement,
pages_read_user_content, pages_manage_metadata and pages_manage_ads.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Em **Select Accounts**, pare na lista de Páginas, mostrando nome, categoria e seguidores.
  3. Marque uma Página, clique em **Connect Selected** e mostre o card da Página em **Connected accounts** (Contas conectadas).

#### `business_management`

- **Para que serve:** Páginas e contas de anúncios que pertencem a um portfólio empresarial. Sem ela, `GET /me/accounts` omite as Páginas de uma empresa, e `GET /me/adaccounts?fields=...,business{id,name}` e `GET /{ad-account}/adspixels` não leem os ativos do portfólio.
- **Texto para enviar:**

```
Most of our customers are businesses and agencies whose Pages and ad accounts
belong to a Meta business portfolio. Connekt Flow needs business_management so
those business-owned Pages appear in the account picker, and so the Meta Ads
settings screen can list the portfolio's ad accounts and Pixels for the customer
to choose from. We do not create, claim or change business assets. It is
requested as a dependency of pages_show_list, pages_messaging, ads_management
and ads_read.
```

- **Gravação:** a exigência da Meta para esta permissão é a de anúncios, então use o fluxo do Meta Ads.
  1. Barra lateral → **Social Accounts** → card **Meta Ads** → **Connect** → **Continue**, e mostre a tela de permissões do diálogo de login.
  2. Na tela de configurações do Meta Ads, mostre a lista **Ad account** (Conta de anúncios) com o nome da empresa ao lado de cada conta, a lista **Pixel** e a lista **Facebook Page** (Página do Facebook). Escolha um de cada e clique em **Save** (Salvar).
  3. Barra lateral → **Ads** (Anúncios). Mostre os totais: valor gasto, impressões, cliques no link, alcance.

#### `pages_read_engagement`

- **Para que serve:** ler o conteúdo e os metadados da própria Página: `GET /{page}?fields=followers_count`, `GET /{page}/feed` (importação de histórico, `providers/meta_history.py`), `GET /{post}?fields=message,full_picture,permalink_url,shares,comments.summary,reactions.summary`, `GET /{video}?fields=post_id,permalink_url`.
- **Texto para enviar:**

```
Connekt Flow reads the posts a Page has published, with their text, picture,
permalink and reaction, comment and share counts, so the Page's managers can see
their content and its engagement in the Reports screen, including posts that
were published outside Connekt Flow and imported by the user. We also read the
Page's follower count to show it on the account card.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Barra lateral → **Reports** (Relatórios) → escolha a Página.
  3. No aviso sobre publicações que ainda não estão aqui, clique em **Import** (Importar).
  4. Em **All posts** (Todas as publicações), mostre os posts importados e clique em um: a gaveta mostra o texto, a imagem e os números do post.

#### `pages_read_user_content`

- **Para que serve:** os comentários que as pessoas deixam nos posts da Página: `GET /{page}/feed?fields=...comments{id,message,created_time,from,parent,permalink_url}`, `GET /{post}/comments` e os campos de webhook `feed` e `mention`.
- **Texto para enviar:**

```
Connekt Flow brings the comments that people write on a Page's posts into a
shared inbox, so the business's team can read, assign and answer them without
opening Facebook. We read the comment text, time, author name and the post it
belongs to. Comments are shown only to the members of the workspace that
connected the Page.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Em outra aba, como uma segunda pessoa, comente em um post da Página.
  3. No Connekt Flow, barra lateral → **Inbox** → filtro **Comments** (Comentários).
  4. Abra o comentário: mostre o texto, o autor e a prévia do post.

#### `pages_manage_posts`

- **Para que serve:** publicar e excluir conteúdo da Página: `POST /{page}/feed`, `/photos`, `/videos`, `/video_reels`, `/video_stories`, `/photo_stories` e `DELETE /{post}`.
- **Texto para enviar:**

```
Connekt Flow's main feature is composing, scheduling and publishing posts. With
pages_manage_posts our users publish text, link, photo, multi-photo, video, Reel
and Story posts to the Pages they manage, immediately or at a scheduled time,
often after an internal approval step. They can also delete a published post
from the Page from inside Connekt Flow.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Barra lateral → **Add new** (Adicionar novo) → **New post** (Novo post).
  3. Em **Where to publish** (Onde publicar), selecione a Página com o formato **Feed**. Digite o texto, anexe uma imagem e clique em **Publish now** (Publicar agora).
  4. Barra lateral → **Calendar** (Calendário). Abra o post e mostre o status **Published** (Publicado).
  5. Abra a Página no facebook.com em outra aba e mostre o post.
  6. De volta ao calendário, abra o post → **Delete** (Excluir). Mantenha marcado **Also delete it from Facebook** (Excluir também do Facebook) e confirme. Recarregue a Página no facebook.com e mostre que o post sumiu.
- **Risco:** o texto da Meta pede "creates, edits, and deletes". O app não edita um post que já está no Facebook. Diga isso nas observações ("Connekt Flow creates and deletes Page posts; it does not edit published posts") ou implemente a edição antes de enviar.

#### `pages_manage_engagement`

- **Para que serve:** escrever comentários como a Página: `POST /{post}/comments` (o primeiro comentário do composer) e `POST /{comment}/comments` (respostas do inbox e das automações).
- **Texto para enviar:**

```
Connekt Flow lets a Page's team answer the comments on their posts from a shared
inbox, and add a first comment to a post at publishing time. Businesses also set
up rules that reply to comments containing a keyword they choose. All comments
are published as the Page, by the Page's own managers.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Barra lateral → **Inbox** → **Comments**. Abra um comentário que um cliente deixou em um post da Página.
  3. Na aba **Public reply** (Resposta pública), digite a resposta e clique em **Reply to comment** (Responder ao comentário).
  4. Abra o post no facebook.com e mostre a resposta abaixo do comentário do cliente.

#### `pages_manage_metadata`

- **Para que serve:** inscrever a Página nos webhooks do app: `POST`, `GET` e `DELETE /{page}/subscribed_apps` com `feed`, `mention`, `messages`. A conexão do Instagram usa a mesma chamada para acrescentar `messages` à Página vinculada.
- **Texto para enviar:**

```
When a user connects a Page, Connekt Flow subscribes that Page to our webhooks
(feed, mention and messages) so new comments, mentions and Messenger messages
reach the user's inbox in real time. We remove the subscription when the user
disconnects the Page. We do not change any other Page setting.
```

- **Gravação:**
  1. Faça a sequência de login. A inscrição acontece quando a Página é conectada, então adicione a legenda "Connecting the Page subscribes it to Connekt Flow webhooks".
  2. Mostre o card da Página com **Messages active** (Mensagens ativas).
  3. Como uma segunda pessoa, comente em um post da Página. Sem recarregar, mostre o comentário chegando no **Inbox**.
  4. De volta a **Social Accounts**, abra **More actions** (Mais ações) no card → **Disconnect** (Desconectar), com a legenda "Disconnecting removes the webhook subscription".

#### `pages_messaging`

- **Para que serve:** conversas do Messenger: `GET /{page}/conversations`, `GET /{conversation}/messages`, `POST /{page}/messages`, `GET /{psid}` para o nome do cliente, e o campo de webhook `messages`.
- **Texto para enviar:**

```
Connekt Flow gives a business one inbox for its customer conversations. With
pages_messaging the Page's team reads the Messenger messages customers send to
the Page and replies to them from Connekt Flow, within the 24-hour window.
Businesses can also send an automatic private reply to a person who commented on
a post with a keyword the business chose. We never send promotional messages or
start conversations.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Como uma segunda pessoa, envie uma mensagem para a Página no Messenger.
  3. No Connekt Flow, barra lateral → **Inbox** → filtro de tipo **Inbox**. Abra a mensagem.
  4. Na aba **Reply** (Resposta), digite a resposta e clique em **Send message** (Enviar mensagem).
  5. Na mesma gravação, mostre o Messenger do lado da segunda pessoa recebendo a resposta.
- **Antes de enviar:** o painel também pede uma Página de teste para esta permissão (etapa `test_page`).

#### `read_insights`

- **Para que serve:** métricas da Página e dos posts: `GET /{page}/insights` (`page_media_view`, `page_total_media_view_unique`, `page_daily_follows_unique`, `page_follows`, `page_post_engagements`) e `GET /{post}/insights` (`post_media_view`, `post_total_media_view_unique`, `post_clicks`, `post_reactions_by_type_total`).
- **Texto para enviar:**

```
Connekt Flow's Reports screen shows each connected Page's performance: views,
reach, reactions, comments, shares, link clicks, new followers and engagement
rate, for a period the user picks, plus the same numbers for each post. Our
users use it to decide what to publish next and to report results to their own
clients.
```

- **Gravação:**
  1. Faça a sequência de login.
  2. Barra lateral → **Reports** → escolha a Página.
  3. Mostre os cards de métricas e o gráfico, depois troque o período de **30 days** (30 dias) para **7 days** (7 dias).
  4. Em **All posts**, clique em um post e mostre os números de **Performance** (Desempenho).

### 5.2 Instagram (pelo Login do Facebook)

Toda gravação de Instagram começa com a sequência de login no card do **Instagram**, para o diálogo mostrar a conta do Instagram sendo selecionada.

#### `instagram_basic`

- **Para que serve:** o perfil e as mídias da conta: `GET /me/accounts?fields=id,instagram_business_account`, `GET /{ig-user}?fields=id,username,name,profile_picture_url,followers_count,media_count`, `GET /{ig-user}/media`, `GET /{media}`, `GET /{ig-user}/tags`.
- **Texto para enviar:**

```
Connekt Flow reads the username, name, profile picture, follower count and media
count of the Instagram professional account the user connects, and shows them on
the "Select Accounts" screen and on the account card in "Social Accounts". We
also read the account's own media (caption, type, permalink, date, like and
comment counts) to list its posts in the Reports screen.
```

- **Gravação:**
  1. Faça a sequência de login no card do Instagram, parando na etapa do diálogo que seleciona a conta do Instagram.
  2. Em **Select Accounts**, mostre a foto, o usuário e os seguidores da conta. Conecte.
  3. Mostre o card da conta com nome, usuário e número de seguidores.

#### `instagram_content_publish`

- **Para que serve:** `POST /{ig-user}/media` (imagem, item de carrossel, Reel, Story), `GET /{container}?fields=status_code,status`, `POST /{ig-user}/media_publish`.
- **Texto para enviar:**

```
Connekt Flow's users compose, schedule and publish photos, carousels, Reels and
Stories to the Instagram professional accounts they manage, with optional
collaborators, tagged people, Reel cover and a first comment. Posts go out
immediately or at the time the user scheduled, often after an internal approval
step.
```

- **Gravação:**
  1. Faça a sequência de login no card do Instagram.
  2. Barra lateral → **Add new** → **New post**. Selecione a conta do Instagram com **Feed**, anexe uma foto, digite a legenda e clique em **Publish now**.
  3. Barra lateral → **Calendar**. Abra o post e mostre o status **Published**.
  4. Abra o perfil no instagram.com e mostre o novo post.

#### `instagram_manage_comments`

- **Para que serve:** `GET /{media}/comments`, `POST /{media}/comments` (primeiro comentário), `POST /{comment}/replies`, `POST /{ig-user}/mentions`, `GET /{ig-user}?fields=mentioned_comment...` e `mentioned_media...`, `GET /{ig-user}/tags`, e `POST /{ig-user}/subscribed_apps` para `comments` e `mentions`.
- **Texto para enviar:**

```
Connekt Flow brings the comments on an Instagram professional account's posts,
and the posts and comments that @mention or tag the account, into a shared
inbox. The account's team reads them and replies from Connekt Flow. Businesses
can also add a first comment when publishing and set up rules that reply to
comments containing a keyword they chose.
```

- **Gravação:**
  1. Faça a sequência de login no card do Instagram.
  2. Como uma segunda pessoa, comente em um post da conta.
  3. Barra lateral → **Inbox** → **Comments**. Abra o comentário, digite em **Public reply** e clique em **Reply to comment**.
  4. Abra o post no instagram.com e mostre a resposta.
- **Observação:** o texto de exigência da própria Meta para esta permissão é uma cópia do de publicação. Gravar o fluxo de comentário acima junto com a etapa de publicação de `instagram_content_publish` atende às duas leituras.

#### `instagram_manage_insights`

- **Para que serve:** `GET /{ig-user}/insights` (`reach`, `views`, `accounts_engaged`, `total_interactions`), `GET /{media}/insights` (`reach`, `views`, `likes`, `comments`, `saved`, `shares`, `total_interactions`) e Business Discovery (`GET /{ig-user}?fields=business_discovery.username(...)`) no Radar.
- **Texto para enviar:**

```
Connekt Flow's Reports screen shows the performance of each connected Instagram
professional account: reach, views, likes, comments, saves, shares, followers
and engagement rate for a chosen period, and the same numbers per post. In the
Radar screen the user can also follow other professional accounts, such as
competitors, and see their public follower count, post count and recent posts
with likes and comments, read through Business Discovery on behalf of the user's
own account.
```

- **Gravação:** a Meta pede as duas partes.
  1. Faça a sequência de login no card do Instagram.
  2. Barra lateral → **Reports** → escolha a conta do Instagram. Mostre os cards e o gráfico, depois clique em um post em **All posts** e mostre os números de **Performance**.
  3. Barra lateral → **Radar** → **Instagram research** (Pesquisa no Instagram) → **Watched profiles** (Perfis acompanhados).
  4. Digite o usuário de outra conta profissional e clique em **Watch** (Acompanhar). Mostre os seguidores, as publicações e as mídias recentes.

#### `instagram_manage_messages`

- **Para que serve:** `GET /{page}/conversations?platform=instagram`, `GET /{conversation}/messages`, `POST /{page}/messages`, `GET /{igsid}` para o nome e o usuário do cliente, e o campo de webhook `messages`.
- **Texto para enviar:**

```
Connekt Flow gives a business one inbox for its customer conversations. With
instagram_manage_messages the account's team reads the Direct messages customers
send, including attachments, and replies from Connekt Flow within the 24-hour
window. Businesses can also send an automatic private reply to a person who
commented on a post with a keyword the business chose. We never start
conversations or send promotional messages.
```

- **Gravação:**
  1. Faça a sequência de login no card do Instagram.
  2. Como uma segunda pessoa, envie uma mensagem no Direct para a conta.
  3. Barra lateral → **Inbox** → filtro de tipo **Inbox**. Abra a mensagem.
  4. Digite a resposta em **Reply** e clique em **Send message**.
  5. Na mesma gravação, mostre o Instagram do lado da segunda pessoa recebendo a resposta.
- **Antes de gravar:** no app do Instagram, ative Configurações → Mensagens e respostas ao story → Controles de mensagens → Ferramentas conectadas → Permitir acesso às mensagens.

#### `instagram_manage_contents`

- **Para que serve:** `DELETE /{media}` quando o usuário exclui um post publicado e mantém marcada a opção de excluir também do Instagram.
- **Texto para enviar:**

```
When a user deletes a post in Connekt Flow that was already published to
Instagram, they can choose to delete it from Instagram as well, so a post
published by mistake is removed in one step instead of leaving a copy online.
```

- **Gravação:**
  1. Faça a sequência de login no card do Instagram, mostrando `instagram_manage_contents` no diálogo.
  2. Publique uma foto como em `instagram_content_publish` e mostre no instagram.com.
  3. Barra lateral → **Calendar** → abra o post → **Delete**. Mantenha marcado **Also delete it from Instagram** (Excluir também do Instagram) e confirme.
  4. Recarregue o perfil no instagram.com e mostre que o post sumiu.
- **Bloqueio:** veja o item 6 da seção 2. A opção só aparece com a configuração de produção ligada.

### 5.3 Threads

O Threads tem login próprio (`https://www.threads.com/oauth/authorize`) e host de API próprio, `https://graph.threads.net/v1.0`. Toda gravação de Threads começa no card do **Threads**: **Connect** → **Continue** → login do Threads → tela de permissões → card da conta. Não há seletor de contas.

#### `threads_basic`

- **Para que serve:** `GET /me?fields=id,username,name,threads_profile_picture_url,threads_biography` e `GET /{container}?fields=status,error_message` durante a publicação.
- **Texto para enviar:**

```
Connekt Flow reads the id, username, name and profile picture of the Threads
profile the user connects, to show it on the account card and in the composer's
list of destinations. The posts a user publishes to Threads through Connekt Flow
are listed, with their text and media, in the calendar and in the Reports
screen, visible only to the members of that user's workspace.
```

- **Gravação:**
  1. Conecte o perfil do Threads, mostrando a tela de permissões.
  2. Mostre o card da conta com nome, usuário e foto.
  3. Barra lateral → **Reports** → escolha a conta do Threads. Em **All posts**, clique em um post e mostre o texto e a mídia.
- **Risco:** a Meta pede "retrieve a list of the user's own Threads posts". O app lista apenas o que foi publicado por ele; não lê os posts do perfil no Threads (`GET /me/threads` não é chamado). Se a Meta recusar, a importação de histórico que existe para Facebook e Instagram precisa ser estendida ao Threads.

#### `threads_content_publish`

- **Para que serve:** `POST /{user}/threads` (texto, imagem, vídeo, itens de carrossel e o contêiner do carrossel) e `POST /{user}/threads_publish`.
- **Texto para enviar:**

```
Connekt Flow's users compose, schedule and publish text, image, video and
carousel posts to their Threads profile, from the same composer they use for
their other social profiles, immediately or at a scheduled time.
```

- **Gravação:**
  1. Conecte o perfil do Threads, mostrando a tela de permissões.
  2. Barra lateral → **Add new** → **New post**. Selecione o perfil do Threads, digite o texto, anexe uma imagem e clique em **Publish now**.
  3. Barra lateral → **Calendar**. Abra o post e mostre o status **Published**.
  4. Abra o perfil no threads.com e mostre o post.

#### `threads_manage_insights`

- **Para que serve:** `GET /{post}/insights?metric=views,likes,replies,reposts,quotes`.
- **Texto para enviar:**

```
Connekt Flow's Reports screen shows how each post a user published to Threads
performed: views, likes, replies, reposts and quotes, and the totals for the
period the user picks. Our users use it to compare their Threads results with
their other social profiles.
```

- **Gravação:**
  1. Conecte o perfil do Threads, mostrando a tela de permissões.
  2. Barra lateral → **Reports** → escolha a conta do Threads. Mostre os cards do período.
  3. Em **All posts**, clique em um post e mostre os números de **Performance**.
- **Risco:** a Meta pede métricas do perfil (seguidores, alcance) e métricas por post. O provedor lê apenas métricas por post; não há chamada a `GET /{user}/threads_insights`. Os cards da tela de Relatórios são totais dos posts.

#### `threads_manage_replies`

- **Para que serve:** o primeiro comentário do composer, publicado como resposta ao próprio post do usuário: `POST /{user}/threads` com `reply_to_id` e depois `POST /{user}/threads_publish`.
- **Texto para enviar:**

```
When publishing to Threads, a Connekt Flow user can write a first comment that
is posted as a reply to their own new post, for example to add a link or extra
context. We only create replies to the user's own posts, on the user's behalf.
We do not hide replies or change reply controls.
```

- **Gravação:**
  1. Conecte o perfil do Threads, mostrando a tela de permissões.
  2. Barra lateral → **Add new** → **New post**. Selecione o perfil do Threads e digite o texto.
  3. Preencha **First Comment** (Primeiro comentário) e clique em **Publish now**.
  4. Abra o post no threads.com e mostre a resposta abaixo dele.

### 5.4 WhatsApp

O WhatsApp conecta pelo Embedded Signup: **Social Accounts** → card **WhatsApp** → **Connect** → **Continue** abre a janela da Meta, onde a pessoa escolhe a conta do WhatsApp Business e o número.

#### `whatsapp_business_management`

- **Para que serve:** trocar o código do cadastro por um token da empresa (`GET /oauth/access_token`), `GET /{waba}/phone_numbers`, e `POST`, `GET` e `DELETE /{waba}/subscribed_apps`.
- **Texto para enviar:**

```
A business connects its WhatsApp Business Account to Connekt Flow through
Embedded Signup. We use whatsapp_business_management to confirm that the phone
number the business chose belongs to that WhatsApp Business Account and to
subscribe the account to our webhooks, so the customer messages it receives
reach the business's inbox in Connekt Flow. We remove the subscription when the
business disconnects.
```

- **Gravação:**
  1. Barra lateral → **Social Accounts** → **WhatsApp** → **Connect** → **Continue**. Percorra a janela do Embedded Signup, escolhendo a conta e o número.
  2. Mostre a mensagem de sucesso e o card do WhatsApp com o número.
  3. Abra o WhatsApp Manager dessa conta e crie um modelo de mensagem, já que a Meta pede ver um sendo criado "on your app or the WhatsApp Manager".
- **Risco:** o app não cria nem lista modelos. É o passo 3 que atende à exigência da Meta.

#### `whatsapp_business_messaging`

- **Para que serve:** `POST /{phone-number}/messages`, `POST /{phone-number}/register`, `GET /{phone-number}?fields=id,display_phone_number,verified_name,platform_type,quality_rating`, `GET /{phone-number}/whatsapp_business_profile`, `GET /{media}` para baixar anexos recebidos, e o campo de webhook `messages`.
- **Texto para enviar:**

```
Connekt Flow gives a business one inbox for its customer conversations. With
whatsapp_business_messaging the business's team receives the WhatsApp messages
customers send to its number, including attachments, and replies to them from
Connekt Flow within the 24-hour customer service window. We register the number
on the Cloud API when it is connected and show its display name and number on
the account card. Connekt Flow does not send bulk or marketing messages.
```

- **Gravação:**
  1. Conecte o número como acima.
  2. De um celular pessoal, envie uma mensagem de WhatsApp para o número da empresa.
  3. Barra lateral → **Inbox** → canal **WhatsApp**. Abra a mensagem.
  4. Digite a resposta em **Reply** e clique em **Send message**.
  5. Na mesma gravação, mostre o WhatsApp do celular pessoal (ou o WhatsApp Web) recebendo a resposta.

### 5.5 Meta Ads

O Meta Ads conecta no seu próprio card: **Social Accounts** → **Meta Ads** → **Connect** → **Continue** → login do Facebook. Ele pede `ads_management`, `ads_read`, `business_management`, `pages_show_list` e `pages_manage_ads`. Depois do login, a tela de configurações pede a conta de anúncios, o Pixel e a Página.

#### `ads_read`

- **Para que serve:** `GET /{campanha ou conta de anúncios}/insights`, com detalhamento por idade e sexo e por posicionamento.
- **Texto para enviar:**

```
Connekt Flow shows a business the results of its Meta ad campaigns next to its
organic social results. With ads_read we read the insights of the ad account the
user selected (spend, impressions, reach, link clicks, CTR, cost per click,
purchases, leads and ROAS), per campaign, per ad, and broken down by age and
gender and by placement, for the period the user picks.
```

- **Gravação:**
  1. Conecte o Meta Ads, mostrando a tela de permissões, e escolha a conta de anúncios.
  2. Barra lateral → **Ads**. Mostre os totais e a tabela de campanhas.
  3. Abra o menu de uma campanha → **See campaign details** (Ver detalhes da campanha). Mostre os detalhamentos por anúncios, idade e sexo, e posicionamentos.

#### `ads_management`

- **Para que serve:** `GET /me/adaccounts`, `GET /{ad-account}/campaigns`, `/{campaign}/adsets`, `/{campaign}/ads`; `POST /{ad-account}/campaigns`, `/adsets`, `/adimages`, `/adcreatives`, `/ads`; `POST /{objeto}` para renomear, pausar, ativar, arquivar e mudar o orçamento; `DELETE /{objeto}` apenas para desfazer uma criação que falhou no meio.
- **Texto para enviar:**

```
Connekt Flow lets a business create and manage simple Meta ad campaigns without
leaving the tool it uses for its organic posts. The user picks an objective,
daily budget, period, audience (countries, age, gender), image, text and
destination link, and we create the campaign, ad set, creative and ad, always
paused so the user reviews it before spending. The user can then rename, pause,
activate or archive a campaign and change an ad set's daily budget.
```

- **Gravação:**
  1. Conecte o Meta Ads, mostrando a tela de permissões, e escolha a conta de anúncios e a Página.
  2. Barra lateral → **Ads** → **New campaign** (Nova campanha). Preencha o formulário e clique em **Create paused campaign** (Criar campanha pausada).
  3. Mostre a nova campanha na tabela e depois a tela de resultados dela.
  4. Abra **Edit campaign** (Editar campanha): renomeie, mude o orçamento diário e clique em **Save**.
  5. Abra o menu da campanha → **Open in Meta Ads Manager** (Abrir no Gerenciador de Anúncios da Meta) e mostre a mesma campanha lá.

#### `pages_manage_ads`

- **Para que serve:** publicar o anúncio como a Página: o `object_story_spec.page_id` do criativo em `POST /{ad-account}/adcreatives`.
- **Texto para enviar:**

```
An ad created in Connekt Flow is published as the business's Facebook Page. With
pages_manage_ads we create the ad creative on behalf of the Page the user
selected in the Meta Ads settings screen.
```

- **Gravação:** a gravação de `ads_management`, com a legenda "The ad is created as the Facebook Page selected in settings" no passo 2, e a **Preview** (Prévia) do anúncio mostrada no final, com o nome da Página.

#### Marketing API Access Tier

- **Para que serve:** os limites de requisições e o número de contas de anúncios que o app pode atender. Antes se chamava "Ads Management Standard Access".
- **Exigência:** 500 chamadas bem-sucedidas à API de Marketing nos últimos 15 dias, com taxa de erro abaixo de 15% nas últimas 500. Usar as telas de Anúncios todos os dias com uma conta de anúncios real chega lá: cada visita à lista de campanhas e aos detalhes de uma campanha faz várias chamadas.
- **Texto para enviar:**

```
Connekt Flow manages ad accounts on behalf of many businesses, each with its own
campaigns and reports. We request full access to the Marketing API so those
businesses can read their results and manage their campaigns without hitting the
development rate limits.
```

- **Gravação:** não é pedida; o painel lista apenas o caso de uso, as chamadas de teste e as perguntas de tratamento de dados.

## 6. Recursos que o código usa

Um recurso é solicitado separadamente das permissões. Nenhum destes está no rascunho.

| Recurso | Tela que depende dele | Estado no app | O que fazer |
|---|---|---|---|
| **Instagram Public Content Access** | Radar → Pesquisa no Instagram → **Hashtags** (`GET /ig_hashtag_search`, `/{hashtag}/top_media`, `/recent_media`) | Não configurado. Item opcional do caso de uso do Instagram | Adicionar no caso de uso do Instagram e solicitar, ou aceitar que a aba Hashtags fique desligada para os clientes |
| **Page Public Metadata Access** | Seletor de local do composer para Facebook e Instagram (`GET /pages/search`) | Não está em nenhum caso de uso que o app tem ou pode adicionar | Perguntar ao suporte da Meta como solicitar para este tipo de app, ou deixar o seletor desligado: ele já avisa o usuário de que a busca não está liberada |
| **Human Agent** | Responder a uma mensagem do Messenger ou do Instagram entre 24 horas e 7 dias depois de o cliente escrever | Não configurado. Item opcional dos casos de uso do Messenger e do Instagram. A configuração do app está desligada | Solicitar só quando a resposta em 7 dias for desejada, e então ligar `PLATFORM_FACEBOOK_HUMAN_AGENT_ENABLED` |
| **Business Asset User Profile Access** | O nome do cliente no inbox e nas automações (`GET /{psid}` e `GET /{igsid}`) | Não configurado. Item opcional dos casos de uso de Páginas, Messenger e Instagram | A referência de perfil do Messenger da Meta lista este recurso como exigido para `name`. Solicitar junto com as permissões de mensagens |

### Texto e gravação de cada um

**Instagram Public Content Access**

Texto para enviar:

```
In Connekt Flow's Radar screen, a business searches a hashtag to see the top and
recent public posts that use it. Our users do this to follow their own campaign
hashtags, find the entrants of their contests and understand what their audience
is posting about before planning their content. We show the caption, media,
date, like and comment counts and a link to the post on Instagram.
```

Gravação: barra lateral → **Radar** → **Instagram research** → aba **Hashtags**. Digite uma hashtag, escolha **Most popular** (Mais populares), clique em **Search** (Buscar), mostre a grade e abra um resultado no Instagram.

**Page Public Metadata Access**

Texto para enviar:

```
When composing a post, a Connekt Flow user can add a location. We use the Pages
Search API to find the place by name and show its name and address so the user
picks the right one. The chosen place is attached to the Facebook or Instagram
post being published.
```

Gravação: no composer, selecione uma Página, abra **Customize** (Personalizar) → **Location** (Localização), digite o nome de um local, escolha um resultado, publique e mostre o post no Facebook com o local.

**Human Agent**

Texto para enviar:

```
Some customer questions cannot be answered within 24 hours, for example when
they arrive on a weekend or need a check with another team. With Human Agent, a
person on the business's team can answer from the Connekt Flow inbox up to 7
days after the customer's message. Automated replies in Connekt Flow never use
this tag.
```

Gravação: abra no inbox uma mensagem do Messenger ou do Instagram com mais de 24 horas, mostre o aviso sobre a resposta de atendimento humano, digite e envie a resposta e mostre-a chegando do lado do cliente.

**Business Asset User Profile Access**

Texto para enviar:

```
Connekt Flow shows the name of the person who sent a message to the business in
the inbox, so the team knows who they are talking to, and lets the business use
the person's first name in its automatic replies.
```

Gravação: a gravação de `pages_messaging`, parando no cabeçalho da conversa, onde aparece o nome do cliente.

## 7. Ordem do trabalho

1. Resolver a seção 2: URL de exclusão de dados, e-mail de contato, a decisão sobre `instagram_manage_contents`, os itens não usados do rascunho, os recursos.
2. Conectar todas as contas com um login que tenha papel no app e executar cada roteiro uma vez, sem gravar. Isso faz as chamadas de teste.
3. Usar as telas de Anúncios todos os dias até atingir a contagem de chamadas da API de Marketing.
4. Esperar 2 dias e conferir no painel que cada permissão mostra a sua chamada de teste.
5. Gravar, um arquivo por permissão, e colar os textos.
6. Preencher as instruções para o revisor da seção 4 e enviar. Manter o app em modo de desenvolvimento até a aprovação.
