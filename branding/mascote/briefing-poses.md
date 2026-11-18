# Moti — briefing de poses

Como pedir uma pose nova do Moti para um card ou tela, e as poses já pensadas para a área de criação (Ideias, Modelos, Datas comemorativas). Use como referência para qualquer lugar novo onde o Moti apareça.

## Índice das poses

| Nº | Tela | Arquivo | Situação |
|---|---|---|---|
| 1 | Ideias | `moti-ideia` | Em uso (banner de Ideias) |
| 2 | Modelos | `moti-modelos` | Em uso (banner de Modelos) |
| 3 | Datas comemorativas | `moti-datas` | Em uso (banners de Datas e do Calendário) |
| 4 | Tarefas | `moti-tarefas` | Em uso (banner de Tarefas) |
| 5 | Dashboard | `moti-dashboard` | Em uso (banner do Dashboard) |
| 6 | Conectar nova rede | `moti-conectar` | Em uso (Contas sociais e avisos de conectar) |
| 7 | Arquivo não encontrado | `moti-nao-encontrado` | Em uso (página 404) |
| 8 | Moti no celular | `moti-celular` | Pronta, sem tela |
| 9 | Moti no notebook | `moti-notebook` | Pronta, sem tela (original em `poses/Robô Moti Digitando no Laptop.png`) |
| 10 | Inbox | `moti-inbox` | Em uso (banner da Inbox) |
| 11 | Automação | `moti-automacao` | Aguardando imagem (o banner usa `moti-computador` até lá) |
| 12 | Contatos | `moti-contatos` | Em uso (banner de Contatos) |
| 13 | Relatórios | `moti-relatorios` | Em uso (banner de Relatórios) |
| 14 | Contas sociais | `moti-contas` | Em uso (banner de Contas sociais) |
| 15 | Galeria | `moti-galeria` | Em uso (banner da Galeria) |
| 16 | Radar | `moti-radar` | Em uso (banner do Radar) |
| 17 | Shopee Afiliados | `moti-shopee` | Em uso (banner de Shopee Afiliados) |
| 18 | Pesquisa no Instagram | `moti-pesquisa` | Em uso (banner da Pesquisa no Instagram) |

## 1. Regras para toda pose

- **Posição na cena:** o Moti fica à direita do card, olhando ou apontando para a esquerda, na direção do título e do botão. Ele conduz o olhar até a ação.
- **Formato:** PNG com fundo transparente de verdade aqui em `mascote/poses/` (alguns geradores entregam o quadriculado cinza e branco desenhado na imagem, sem transparência; nesse caso peça de novo ou recorte o fundo antes de converter). Para o sistema, converter para WebP em `static/img/moti/`. Sem sombra no chão (a sombra é aplicada no código).
- **Tamanho:** cerca de 400 px de altura e 300 a 400 px de largura na versão WebP. No card ele aparece com cerca de 150 px de altura; o dobro deixa nítido em telas retina.
- **Enquadramento:** corpo inteiro, ou da cintura para cima quando o objeto precisa de destaque. Pouca margem vazia em volta (cerca de 10 px), porque a imagem é encaixada encostada no lado direito do card.
- **Estilo:** o mesmo das poses atuais (`moti_pose_joinha_corpo_inteiro`, `moti_trabalhando_no_computador`): acabamento 3D branco e roxo, viseira escura com olhos lilases, capa roxa.
- **Cores dos objetos:** roxo, lilás ou branco. Nada fora da paleta da marca.
- **Leitura em tamanho pequeno:** o objeto em destaque (lâmpada, cartões, calendário) precisa ser reconhecível a 150 px de altura. Prefira objetos grandes e simples.

## 2. Poses da área de criação

### Pose 1 — Ideias — "Nunca fique sem ideias"

- **Arquivo:** `moti-ideia`
- **Pose:** Moti segurando uma lâmpada acesa (roxa, brilhando) acima da cabeça com uma das mãos; a outra mão num gesto de "eureka", com o dedo indicador levantado.
- **Expressão:** olhos em arco, feliz e surpreso, o "tive uma ideia!".
- **Detalhes:** dois ou três tracinhos de brilho saindo da lâmpada. Opcional: um post-it flutuando ao lado.

### Pose 2 — Modelos — "Comece com um modelo pronto"

- **Arquivo:** `moti-modelos`
- **Pose:** Moti segurando dois ou três cartões em leque, como cartas de baralho, mostrando-os para a esquerda. Os cartões imitam posts: um quadradinho de imagem e linhas de texto.
- **Expressão:** confiante, meio sorriso de "escolhe um".
- **Detalhes:** cartões em branco e lilás.

### Pose 3 — Datas comemorativas — "Esteja pronto para cada data comemorativa"

- **Arquivo:** `moti-datas`
- **Pose:** Moti abraçando ou segurando um calendário de mesa (folha com argolas em cima e um dia marcado com um círculo roxo), usando um chapéu de festa.
- **Expressão:** animado, com o braço livre comemorando.
- **Detalhes:** poucos confetes roxos e lilases caindo em volta, sem poluir.

## 3. Poses de outras telas

### Pose 4 — Tarefas — "Mantenha o trabalho do time andando"

- **Arquivo:** `moti-tarefas` (em uso no card de Tarefas; original em `poses/Robô Mascote com Checklist Roxo.png`)
- **Pose:** Moti de corpo inteiro, em passo firme para a esquerda, segurando com uma mão uma prancheta grande virada para a frente. Na prancheta, uma checklist de três linhas: as duas primeiras com um visto roxo, a terceira sendo marcada naquele momento por um lápis grande na outra mão.
- **Expressão:** determinado e satisfeito, o "deixa comigo, está andando".
- **Detalhes:** um selo redondo de visto (✓) roxo flutuando ao lado da prancheta, com dois tracinhos de brilho. Nada mais em volta.
- **Leitura a 150 px:** a prancheta é o objeto principal e precisa ocupar cerca de um terço da altura do Moti; os vistos são grossos, para continuarem legíveis pequenos.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: card de Tarefas, "Mantenha o trabalho do time andando"
Arquivo: moti-tarefas
Pose: corpo inteiro, dando um passo firme para a esquerda; numa mão segura uma
prancheta grande virada para a frente com uma checklist de três linhas — as duas
primeiras com visto roxo, a terceira sendo marcada agora por um lápis grande que
ele segura na outra mão
Expressão: determinado e satisfeito, "deixa comigo"
Detalhes: um selo redondo de visto roxo flutuando ao lado da prancheta, com dois
tracinhos de brilho
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG transparente, sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; prancheta, lápis e selo só em roxo, lilás e branco
```

### Pose 5 — Dashboard — "Sua marca, num relance"

- **Arquivo:** `moti-dashboard` (em uso no card do topo do dashboard; original em `poses/Robô Moti com Tablet e Gráfico Ascendente.png`)
- **Pose:** Moti da cintura para cima, segurando com uma mão um tablet grande virado para a frente. Na tela, um gráfico de linha subindo com a área embaixo preenchida, e três barrinhas ao lado. A outra mão aponta para o ponto mais alto da linha.
- **Expressão:** orgulhoso e confiante, o "olha como a sua marca está indo".
- **Detalhes:** uma setinha roxa para cima saindo do canto do tablet, com dois tracinhos de brilho. Nada mais em volta.
- **Leitura a 150 px:** o tablet ocupa cerca de metade da altura da imagem e o gráfico tem só uma linha grossa e três barras, sem números nem textos.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: card do topo do Dashboard, "Sua marca, num relance"
Arquivo: moti-dashboard
Pose: da cintura para cima; numa mão segura um tablet grande virado para a frente
com um gráfico de linha subindo (área embaixo da linha preenchida) e três barras
ao lado; com a outra mão aponta para o ponto mais alto da linha
Expressão: orgulhoso e confiante, "olha como a sua marca está indo"
Detalhes: uma setinha roxa para cima saindo do canto do tablet, com dois
tracinhos de brilho
Direção: olhando para a esquerda
Enquadramento: cintura para cima
Formato: PNG transparente, sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; tablet branco, gráfico em roxo e lilás, sem números nem
textos na tela
```

### Pose 6 — Conectar nova rede — "Conecte mais uma rede"

- **Arquivo:** `moti-conectar` (em uso no card "Conecte mais uma rede" e no aviso "Ainda não há resultados" do dashboard; original em `poses/Moti Conecta o Cabo Roxo.png`)
- **Pose:** Moti de corpo inteiro encaixando um plugue grande numa tomada redonda que flutua à esquerda dele. Do plugue sai um cabo em curva até a mão dele. Na tomada, um símbolo de "+".
- **Expressão:** animado e prestativo, o "vamos ligar mais uma?".
- **Detalhes:** três tracinhos de faísca roxa no ponto onde o plugue encaixa. Nada mais em volta.
- **Leitura a 150 px:** no card ele aparece pequeno (cerca de 65 px de altura), então plugue e tomada precisam ser grandes e de contorno simples. Não usar logotipos de redes sociais: a tomada com "+" já diz "mais uma rede" e não fica presa a uma marca.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: card "Conecte mais uma rede" do Dashboard
Arquivo: moti-conectar
Pose: corpo inteiro; com as duas mãos encaixa um plugue grande numa tomada
redonda que flutua à esquerda dele, com um símbolo de "+" no centro; do plugue
sai um cabo em curva
Expressão: animado e prestativo, "vamos ligar mais uma?"
Detalhes: três tracinhos de faísca roxa onde o plugue encaixa
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG transparente, sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; plugue, cabo e tomada em roxo, lilás e branco; sem
logotipos de redes sociais
```

### Pose 7 — Arquivo não encontrado — "Não encontramos este arquivo"

- **Arquivo:** `moti-nao-encontrado` (em uso na página de erro 404, e para mídias que sumirem da galeria ou de um post; original em `poses/Robô Curioso Investigando a Pasta.png`; diferente de `moti-procurando`, que é a galeria vazia, e de `moti-sem-conexao`, que é a conta desconectada)
- **Pose:** Moti de corpo inteiro, inclinado sobre uma pasta aberta e vazia, olhando para dentro com uma lupa grande. A outra mão coça a cabeça.
- **Expressão:** confuso, com um toque de "desculpa": os olhos lilases em formato de ponto de interrogação ou um olho maior que o outro.
- **Detalhes:** um ponto de interrogação roxo grande flutuando acima da pasta. Opcional: uma folha de papel com o canto dobrado caindo devagar, vazia.
- **Leitura a 150 px:** a pasta vazia e o ponto de interrogação são o que conta a história, então os dois precisam ser grandes. A lupa pode ser menor.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: tela de arquivo não encontrado (erro 404 e mídia que sumiu)
Arquivo: moti-nao-encontrado
Pose: corpo inteiro, inclinado sobre uma pasta aberta e vazia, olhando para
dentro dela com uma lupa grande numa mão; com a outra mão coça a cabeça
Expressão: confuso e um pouco sem graça, "ué, cadê?"
Detalhes: um ponto de interrogação roxo grande flutuando acima da pasta
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG transparente, sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; pasta, lupa e ponto de interrogação em roxo, lilás e branco
```

### Pose 8 — Moti no celular — "Sua marca no bolso"

- **Arquivo:** `moti-celular` (pronta em `static/img/moti/`, ainda sem tela; para telas e avisos sobre o uso no celular: o app aberto no telefone, avisos que chegam no celular, prévia de como o post aparece no feed; original em `poses/Robô Futurista Curtindo o Coração.png`)
- **Pose:** Moti de corpo inteiro, segurando com as duas mãos um celular grande virado para a frente, como quem mostra a tela. Na tela, um post: um quadrado de imagem lilás, duas linhas de texto e um coração roxo embaixo. O polegar de uma das mãos está tocando a tela.
- **Expressão:** descontraído e feliz, o "dá para fazer tudo daqui".
- **Detalhes:** dois corações roxos pequenos subindo do celular, como numa curtida. Nada mais em volta.
- **Leitura a 150 px:** o celular ocupa cerca de metade da altura do Moti e o post na tela tem só três elementos: imagem, linhas e coração.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: telas e avisos sobre o uso no celular, "Sua marca no bolso"
Arquivo: moti-celular
Pose: corpo inteiro; segura com as duas mãos um celular grande virado para a
frente, mostrando a tela, com o polegar tocando nela; na tela, um post: um
quadrado de imagem lilás, duas linhas de texto e um coração roxo embaixo
Expressão: descontraído e feliz, "dá para fazer tudo daqui"
Detalhes: dois corações roxos pequenos subindo do celular, como numa curtida
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG transparente, sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; celular branco, tela em roxo, lilás e branco, sem
logotipos de redes sociais
```

### Pose 9 — Moti no notebook — "Escreva, agende e pronto"

- **Arquivo:** `moti-notebook` (pronta em `static/img/moti/`, ainda sem tela; original em `poses/Robô Moti Digitando no Laptop.png`; para a criação de posts: o editor vazio, "Nenhum post ainda" e o aviso de post agendado). Diferente de `moti-computador`, que fica no canto do menu lateral trabalhando numa mesa: aqui ele está sentado no chão, à vontade, e o post sai da tela.
- **Pose:** Moti de corpo inteiro, sentado de pernas cruzadas com um notebook aberto no colo, digitando com as duas mãos. Da tela sai, flutuando para a esquerda, um cartão de post (um quadrado de imagem lilás e duas linhas de texto) com um pequeno relógio roxo no canto, de post agendado.
- **Expressão:** concentrado e contente, olhos lilases em meia-lua, o "deixa que eu escrevo".
- **Detalhes:** dois tracinhos de brilho em volta do cartão. A capa cai solta para trás, no chão. Nada mais em volta.
- **Leitura a 150 px:** o notebook é visto de lado, em ângulo de três quartos, com a tampa bem visível; o cartão que sai da tela tem o tamanho da cabeça do Moti, para continuar legível pequeno.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: criação de posts, "Escreva, agende e pronto"
Arquivo: moti-notebook
Pose: corpo inteiro, sentado no chão de pernas cruzadas, com um notebook aberto
no colo, visto em três quartos, digitando com as duas mãos; da tela sai,
flutuando para a esquerda, um cartão de post (quadrado de imagem lilás e duas
linhas de texto) com um pequeno relógio roxo no canto
Expressão: concentrado e contente, "deixa que eu escrevo"
Detalhes: dois tracinhos de brilho em volta do cartão; a capa cai solta no chão
atrás dele
Direção: olhando para a esquerda, para o cartão
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; notebook branco com detalhes roxos, cartão e relógio em
roxo, lilás e branco, sem logotipos de redes sociais
```

## 4. Poses dos banners das outras telas

Uma pose para o banner de cada tela que ainda usa uma pose emprestada de outra. O banner é o mesmo de Ideias e Tarefas (`composer/partials/_ideas_hero.html`): o Moti aparece com cerca de 160 px de altura, à direita, olhando para o título. Quando a imagem chegar, converter para WebP em `static/img/moti/` e trocar a linha da tela no banner.

### Pose 10 — Inbox — "Todas as conversas em um só lugar"

- **Arquivo:** `moti-inbox` (em uso no banner da Inbox; original em `poses/Robô Futurista Gerenciando Mensagens.png`)
- **Pose:** Moti de corpo inteiro, de pé ao lado de uma bandeja de entrada grande (uma caixa aberta, como a de correspondência), segurando com uma mão um balão de conversa que acabou de pegar no ar. Outros dois balões vêm voando da esquerda em direção à bandeja: um com três pontinhos, outro com um coração.
- **Expressão:** atento e acolhedor, o "pode falar, estou aqui".
- **Detalhes:** um pequeno sinal de "@" flutuando junto dos balões. Nada mais em volta.
- **Leitura a 150 px:** a bandeja e os três balões são grandes e de contorno simples; os balões não têm texto, só os pontinhos, o coração e o "@".

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Inbox, "Todas as conversas em um só lugar"
Arquivo: moti-inbox
Pose: corpo inteiro, de pé ao lado de uma bandeja de entrada grande e aberta; com
uma mão pega no ar um balão de conversa; outros dois balões chegam voando da
esquerda até a bandeja, um com três pontinhos e outro com um coração
Expressão: atento e acolhedor, "pode falar, estou aqui"
Detalhes: um pequeno "@" flutuando junto dos balões
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; bandeja e balões em roxo,
lilás e branco, sem textos e sem logotipos de redes sociais
```

### Pose 11 — Automação — "Deixe as respostas saírem sozinhas"

- **Arquivo:** `moti-automacao` (para o banner de Automação, que hoje usa `moti-computador`, a mesma pose do canto do menu lateral)
- **Pose:** Moti de corpo inteiro, encostado de lado num botão redondo grande com um raio roxo, como quem acabou de apertá-lo, de braços cruzados e à vontade. Do botão sai, para a esquerda, uma fila de três balões de resposta, um atrás do outro, indo embora sozinhos.
- **Expressão:** tranquilo e satisfeito, o "pode deixar que sai sozinho".
- **Detalhes:** dois tracinhos de brilho em volta do raio. Nada mais em volta.
- **Leitura a 150 px:** o botão com o raio ocupa cerca de um terço da altura do Moti; os balões são simples e iguais, para lerem como uma fila.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Automação, "Deixe as respostas saírem sozinhas"
Arquivo: moti-automacao
Pose: corpo inteiro, encostado de lado num botão redondo grande com um raio roxo,
de braços cruzados e à vontade; do botão sai para a esquerda uma fila de três
balões de resposta, um atrás do outro, indo embora sozinhos
Expressão: tranquilo e satisfeito, "pode deixar que sai sozinho"
Detalhes: dois tracinhos de brilho em volta do raio
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; botão, raio e balões em roxo,
lilás e branco, sem textos
```

### Pose 12 — Contatos — "Conheça quem fala com a sua marca"

- **Arquivo:** `moti-contatos` (em uso no banner de Contatos; original em `poses/Robô Mascote Roxo com Cartões de Contato.png`)
- **Pose:** Moti de corpo inteiro, segurando com uma mão uma ficha de contato grande virada para a frente: um círculo de avatar no alto e três linhas embaixo. Atrás da ficha aparecem, em leque, as pontas de outras duas fichas. A outra mão acena, como quem cumprimenta alguém.
- **Expressão:** simpático e curioso, o "prazer, quem é você?".
- **Detalhes:** um pequeno coração roxo saindo do canto da ficha. Nada mais em volta.
- **Leitura a 150 px:** a ficha da frente ocupa cerca de um terço da altura do Moti, e o avatar dela é um círculo lilás simples, sem rosto desenhado.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Contatos, "Conheça quem fala com a sua marca"
Arquivo: moti-contatos
Pose: corpo inteiro; numa mão segura uma ficha de contato grande virada para a
frente (um círculo de avatar no alto e três linhas embaixo), com outras duas
fichas em leque atrás dela; a outra mão acena, cumprimentando
Expressão: simpático e curioso, "prazer, quem é você?"
Detalhes: um pequeno coração roxo saindo do canto da ficha
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; fichas em branco e lilás,
avatar como um círculo lilás simples, sem rosto e sem textos
```

### Pose 13 — Relatórios — "Entenda o que funciona"

- **Arquivo:** `moti-relatorios` (em uso no banner de Relatórios; original em `poses/Mascote robô apresentando gráfico de sucesso.png`)
- **Pose:** Moti de corpo inteiro, ao lado de um quadro de apresentação num cavalete, apontando com uma varinha para a barra mais alta de um gráfico de quatro barras. Em cima da barra mais alta, uma estrela roxa.
- **Expressão:** confiante e explicativo, o "olha aqui o que deu certo".
- **Detalhes:** dois tracinhos de brilho em volta da estrela. Nada mais em volta.
- **Leitura a 150 px:** o quadro ocupa cerca de metade da altura da imagem; o gráfico tem só as quatro barras e a estrela, sem números nem eixos.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Relatórios, "Entenda o que funciona"
Arquivo: moti-relatorios
Pose: corpo inteiro, ao lado de um quadro de apresentação num cavalete, apontando
com uma varinha para a barra mais alta de um gráfico de quatro barras, que tem
uma estrela roxa em cima
Expressão: confiante e explicativo, "olha aqui o que deu certo"
Detalhes: dois tracinhos de brilho em volta da estrela
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; quadro branco, barras em roxo
e lilás, sem números, eixos nem textos
```

### Pose 14 — Contas sociais — "Conecte suas redes"

- **Arquivo:** `moti-contas` (em uso no banner de Contas sociais; original em `poses/Robô Futurista Conectando Nós Roxos.png`)
- **Pose:** Moti de corpo inteiro, no centro, segurando com as duas mãos as pontas de três cabos roxos. Cada cabo vai até um círculo flutuante em volta dele (dois à esquerda, um acima), e cada círculo tem um visto (✓) roxo, de conexão funcionando.
- **Expressão:** orgulhoso e cuidadoso, o "tudo ligado e funcionando".
- **Detalhes:** um pequeno brilho em cada ponto onde o cabo encontra o círculo. Nada mais em volta.
- **Leitura a 150 px:** os três círculos são grandes e iguais, com o visto grosso; os cabos são linhas simples, sem nós.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Contas sociais, "Conecte suas redes"
Arquivo: moti-contas
Pose: corpo inteiro, no centro, segurando com as duas mãos as pontas de três cabos
roxos; cada cabo vai até um círculo flutuante em volta dele (dois à esquerda,
um acima), e cada círculo tem um visto roxo
Expressão: orgulhoso e cuidadoso, "tudo ligado e funcionando"
Detalhes: um pequeno brilho onde cada cabo encontra o círculo
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; cabos e círculos em roxo,
lilás e branco, sem logotipos de redes sociais
```

### Pose 15 — Galeria — "Os arquivos da sua marca em um só lugar"

- **Arquivo:** `moti-galeria` (em uso no banner da Galeria; original em `poses/Robô Mascote Organizando Imagens na Pasta.png`)
- **Pose:** Moti de corpo inteiro, guardando uma foto emoldurada numa pasta grande e aberta apoiada no chão. Na pasta já aparecem as pontas de outras fotos e de um quadradinho com o símbolo de "play" (um vídeo).
- **Expressão:** caprichoso e satisfeito, o "cada coisa no seu lugar".
- **Detalhes:** uma foto pequena entrando na pasta pelo alto, como se tivesse acabado de chegar. Nada mais em volta.
- **Leitura a 150 px:** a pasta ocupa cerca de metade da altura do Moti; as fotos têm só um morrinho e um sol, o desenho padrão de imagem, para serem reconhecidas pequenas.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Galeria, "Os arquivos da sua marca em um só lugar"
Arquivo: moti-galeria
Pose: corpo inteiro, guardando uma foto emoldurada numa pasta grande e aberta
apoiada no chão; na pasta aparecem as pontas de outras fotos e de um
quadradinho com o símbolo de "play"
Expressão: caprichoso e satisfeito, "cada coisa no seu lugar"
Detalhes: uma foto pequena entrando na pasta pelo alto
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; pasta, fotos e "play" em roxo,
lilás e branco; fotos com o desenho padrão de imagem (morrinho e sol)
```

### Pose 16 — Radar — "Descubra o que vale a pena postar"

- **Arquivo:** `moti-radar` (em uso no banner do Radar; original em `poses/Robô Futurista em Busca de Ofertas.png`)
- **Pose:** Moti de corpo inteiro, olhando por um binóculo para a esquerda, inclinado para a frente, como quem acabou de avistar algo. À esquerda flutuam as descobertas: uma etiqueta de preço com um "%" e um "#" grande.
- **Expressão:** animado e surpreso, o "achei uma boa!".
- **Detalhes:** três tracinhos de brilho em volta da etiqueta. Nada mais em volta.
- **Leitura a 150 px:** o binóculo é grande e de contorno simples; a etiqueta e o "#" têm tamanho parecido com a cabeça do Moti.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Radar, "Descubra o que vale a pena postar"
Arquivo: moti-radar
Pose: corpo inteiro, inclinado para a frente, olhando por um binóculo para a
esquerda; à esquerda flutuam uma etiqueta de preço com um "%" e um "#" grande
Expressão: animado e surpreso, "achei uma boa!"
Detalhes: três tracinhos de brilho em volta da etiqueta
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; binóculo, etiqueta e "#" em
roxo, lilás e branco, sem textos além do "%" e do "#"
```

### Pose 17 — Shopee Afiliados — "Ofertas que pagam para você postar"

- **Arquivo:** `moti-shopee` (em uso no banner da página Shopee Afiliados, dentro do Radar; original em `poses/Robô Moti e Carrinho de Compras.png`)
- **Pose:** Moti de corpo inteiro, empurrando um carrinho de compras pequeno e cheio de caixinhas de produto. De uma das caixas sai uma moeda roxa grande, e ele aponta para ela com a mão livre.
- **Expressão:** esperto e contente, o "esse aqui rende".
- **Detalhes:** duas moedas pequenas pulando do carrinho, com tracinhos de brilho. Nada mais em volta.
- **Leitura a 150 px:** o carrinho e a moeda grande são o que conta a história; as caixinhas são blocos simples, sem marcas.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner de Shopee Afiliados, "Ofertas que pagam para você postar"
Arquivo: moti-shopee
Pose: corpo inteiro, empurrando um carrinho de compras pequeno e cheio de
caixinhas de produto; de uma das caixas sai uma moeda roxa grande, e ele aponta
para ela com a mão livre
Expressão: esperto e contente, "esse aqui rende"
Detalhes: duas moedas pequenas pulando do carrinho, com tracinhos de brilho
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; carrinho, caixas e moedas em
roxo, lilás e branco, sem logotipos (nem o da Shopee) e sem textos
```

### Pose 18 — Pesquisa no Instagram — "Veja o que funciona para os outros"

- **Arquivo:** `moti-pesquisa` (em uso no banner da Pesquisa no Instagram, dentro do Radar; original em `poses/Robô Criativo com Cartões Sociais.png`)
- **Pose:** Moti de corpo inteiro, de pé diante de três cartões de perfil flutuando à esquerda (cada um com um círculo de avatar e uma fileira de três quadradinhos de posts). Com uma mão segura uma prancheta pequena e com a outra anota algo, olhando para os cartões.
- **Expressão:** curioso e animado, o "olha que ideia boa".
- **Detalhes:** um "#" pequeno e uma lâmpada pequena acesa saindo da prancheta. Nada mais em volta.
- **Leitura a 150 px:** os três cartões são grandes e iguais; os avatares são círculos lilases sem rosto e os posts, quadradinhos lisos.

Pedido pronto para colar no gerador de imagens:

```
Pose do Moti para: banner da Pesquisa no Instagram, "Veja o que funciona para os outros"
Arquivo: moti-pesquisa
Pose: corpo inteiro, de pé diante de três cartões de perfil flutuando à
esquerda (cada um com um círculo de avatar e três quadradinhos de posts); numa
mão segura uma prancheta pequena e com a outra anota algo, olhando os cartões
Expressão: curioso e animado, "olha que ideia boa"
Detalhes: um "#" pequeno e uma lâmpada pequena acesa saindo da prancheta
Direção: olhando para a esquerda
Enquadramento: corpo inteiro
Formato: PNG com fundo transparente de verdade (sem quadriculado desenhado),
sem sombra no chão
Estilo: o mesmo das poses atuais (moti_pose_joinha_corpo_inteiro,
moti_trabalhando_no_computador) — 3D branco e roxo, viseira escura com olhos
lilases, capa roxa; cartões, prancheta, "#" e
lâmpada em roxo, lilás e branco, sem logotipos (nem o do Instagram) e sem textos
```

## 5. Moti com as redes (série `redes/`)

Uma imagem por rede social, com o Moti apresentando o ícone dela: para os cards e avisos de cada rede (conectar, reconectar, relatório da rede). Arquivos em `mascote/redes/moti_rede_<rede>.png`. Diferente do resto deste briefing, aqui o ícone oficial da rede **aparece**, porque a imagem existe para dizer de qual rede se trata.

### O que está errado nas imagens atuais

As 12 imagens de `redes/` foram geradas fora do padrão do personagem e precisam ser refeitas, todas:

- **Logo errada no peito:** um "A" roxo no lugar do emblema da Connekt Flow.
- **Fones sem emblema:** os módulos laterais da cabeça aparecem só com um anel, sem o símbolo aceso no centro.
- **Brilho em volta do corpo:** algumas têm um halo roxo/branco e manchas escuras no fundo transparente (ex.: `moti_rede_instagram`, `moti_rede_youtube`), que sujam o recorte em fundo claro.

### O formato certo (base: `moti_rede_youtube.png`)

A composição de `moti_rede_youtube.png` é a referência de pose e enquadramento para a série toda; só o personagem é corrigido:

- **Pose:** Moti de corpo inteiro, de pé, em três quartos virado para a esquerda. A mão do lado esquerdo da imagem segura, com a palma para cima, o ícone da rede à altura do ombro; a outra mão fica na cintura.
- **Ícone:** o ícone oficial da rede em 3D, quadrado de cantos arredondados, com as cores e o desenho oficiais, mais ou menos do tamanho da cabeça do Moti, levemente inclinado. Três tracinhos de brilho roxos saindo do canto de cima, à esquerda do ícone.
- **Expressão:** olhos em arco e sorriso pequeno, o "olha essa aqui".
- **Capa:** sem capa (configuração sem capa do turnaround).
- **Emblema:** o símbolo da Connekt Flow (`branding/logo/connektflow-symbol.png`) no centro do peito, pequeno e em relevo, e o mesmo símbolo aceso no centro de cada fone. O símbolo é: duas barras horizontais escuras em cima, a de baixo dobrando num ângulo reto e descendo pela direita, e embaixo à esquerda um traço roxo que desce, faz uma diagonal para a esquerda e corre na horizontal para a direita. Nunca uma letra.
- **Formato:** PNG 1536 × 1024 (paisagem) com fundo transparente de verdade, sem halo, sem brilho no fundo, sem sombra no chão e sem manchas em volta. O Moti fica na metade direita e o ícone na metade esquerda, com cerca de 10% de margem nas bordas.

### Pedido base (colar no gerador junto com as imagens)

Anexar sempre três imagens: o turnaround oficial do Moti, o símbolo `branding/logo/connektflow-symbol.png` e `redes/moti_rede_youtube.png` (só como referência de pose). Trocar `<REDE>` e `<ÍCONE>` pela linha da tabela abaixo.

```
Pose do Moti para: série das redes, imagem da rede <REDE>
Arquivo: moti_rede_<arquivo>
Referências anexas: (1) turnaround oficial do Moti = identidade do personagem;
(2) símbolo da Connekt Flow = emblema do peito e dos fones;
(3) moti_rede_youtube.png = APENAS pose e enquadramento (o "A" no peito dessa
imagem está errado, não copiar)
Pose: corpo inteiro, de pé, em três quartos virado para a esquerda; numa mão,
com a palma para cima à altura do ombro, segura à esquerda do corpo o ícone
oficial da <REDE> em 3D: <ÍCONE>; a outra mão fica na cintura
Expressão: olhos em arco e sorriso pequeno, "olha essa aqui"
Detalhes: três tracinhos de brilho roxos saindo do canto de cima do ícone, à
esquerda; nada mais em volta
Capa: sem capa
Emblema: o símbolo da Connekt Flow anexo, com a geometria exata (duas barras
escuras em cima, a de baixo descendo pela direita em ângulo reto, e o traço roxo
embaixo à esquerda), pequeno e em relevo no centro do peito, e o mesmo símbolo
aceso em violeta no centro de cada fone; nunca uma letra "A" nem outro desenho
Direção: olhando para o ícone, à esquerda
Enquadramento: corpo inteiro; Moti na metade direita, ícone na metade esquerda
Formato: PNG 1536 x 1024 com fundo transparente de verdade (sem quadriculado
desenhado), sem halo nem brilho em volta do corpo, sem sombra no chão
Estilo: o mesmo do turnaround — 3D branco perolado e roxo, viseira preta com
olhos lilases; só o ícone da rede leva as cores oficiais dela
```

### Uma linha por rede

| Rede | Arquivo | `<ÍCONE>` |
|---|---|---|
| YouTube | `moti_rede_youtube` | quadrado vermelho com o retângulo arredondado branco e o triângulo de play vermelho |
| Instagram | `moti_rede_instagram` | quadrado com o degradê oficial (amarelo, laranja, rosa, roxo) e a câmera branca em contorno |
| Facebook | `moti_rede_facebook` | quadrado azul (#1877F2) com o "f" branco |
| Threads | `moti_rede_threads` | quadrado preto com o "@" estilizado do Threads em branco |
| TikTok | `moti_rede_tiktok` | quadrado preto com a nota musical do TikTok em branco, com o deslocamento ciano e vermelho |
| X | `moti_rede_x` | quadrado preto com o "X" do X em branco |
| LinkedIn | `moti_rede_linkedin` | quadrado azul (#0A66C2) com o "in" branco |
| Pinterest | `moti_rede_pinterest` | quadrado vermelho (#E60023) com o "P" branco do Pinterest |
| Bluesky | `moti_rede_bluesky` | quadrado azul (#1185FE) com a borboleta branca do Bluesky |
| Mastodon | `moti_rede_mastodon` | quadrado roxo-azulado (#6364FF) com o "m" branco do Mastodon |
| WhatsApp | `moti_rede_whatsapp` | quadrado verde (#25D366) com o balão e o telefone brancos |
| Google Business | `moti_rede_google_business` | quadrado branco com a loja de toldo nas cores do Google (azul, vermelho, amarelo, verde) |

### Antes de aceitar uma imagem

- O peito tem o símbolo da Connekt Flow, e não um "A". Os dois fones também têm o símbolo, aceso.
- Ao abrir o PNG sobre fundo branco e sobre fundo escuro, não aparece halo, mancha nem quadriculado em volta.
- O ícone é o oficial da rede, sem letras inventadas e sem texto.
- Sem capa, corpo inteiro, e o mesmo tamanho e posição do Moti de `moti_rede_youtube.png`, para a série ficar igual lado a lado.

## 6. Modelo de pedido para uma pose nova

Copie e preencha:

```
Pose do Moti para: <tela ou card, e o título dele>
Arquivo: moti-<nome>
Pose: <o que o corpo faz e o que ele segura>
Expressão: <o sentimento, em uma frase>
Detalhes: <elementos em volta, no máximo dois>
Direção: olhando/apontando para a esquerda
Enquadramento: corpo inteiro | cintura para cima
Formato: PNG transparente, sem sombra no chão
```
