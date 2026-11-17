# Moti — briefing de poses

Como pedir uma pose nova do Moti para um card ou tela, e as poses já pensadas para a área de criação (Ideias, Modelos, Datas comemorativas). Use como referência para qualquer lugar novo onde o Moti apareça.

## 1. Regras para toda pose

- **Posição na cena:** o Moti fica à direita do card, olhando ou apontando para a esquerda, na direção do título e do botão. Ele conduz o olhar até a ação.
- **Formato:** PNG com fundo transparente aqui em `mascote/poses/`. Para o sistema, converter para WebP em `static/img/moti/`. Sem sombra no chão (a sombra é aplicada no código).
- **Tamanho:** cerca de 400 px de altura e 300 a 400 px de largura na versão WebP. No card ele aparece com cerca de 150 px de altura; o dobro deixa nítido em telas retina.
- **Enquadramento:** corpo inteiro, ou da cintura para cima quando o objeto precisa de destaque. Pouca margem vazia em volta (cerca de 10 px), porque a imagem é encaixada encostada no lado direito do card.
- **Estilo:** o mesmo das poses atuais (`moti_pose_joinha_corpo_inteiro`, `moti_trabalhando_no_computador`): acabamento 3D branco e roxo, viseira escura com olhos lilases, capa roxa.
- **Cores dos objetos:** roxo, lilás ou branco. Nada fora da paleta da marca.
- **Leitura em tamanho pequeno:** o objeto em destaque (lâmpada, cartões, calendário) precisa ser reconhecível a 150 px de altura. Prefira objetos grandes e simples.

## 2. Poses da área de criação

### Ideias — "Nunca fique sem ideias"

- **Arquivo:** `moti-ideia`
- **Pose:** Moti segurando uma lâmpada acesa (roxa, brilhando) acima da cabeça com uma das mãos; a outra mão num gesto de "eureka", com o dedo indicador levantado.
- **Expressão:** olhos em arco, feliz e surpreso, o "tive uma ideia!".
- **Detalhes:** dois ou três tracinhos de brilho saindo da lâmpada. Opcional: um post-it flutuando ao lado.

### Modelos — "Comece com um modelo pronto"

- **Arquivo:** `moti-modelos`
- **Pose:** Moti segurando dois ou três cartões em leque, como cartas de baralho, mostrando-os para a esquerda. Os cartões imitam posts: um quadradinho de imagem e linhas de texto.
- **Expressão:** confiante, meio sorriso de "escolhe um".
- **Detalhes:** cartões em branco e lilás.

### Datas comemorativas — "Esteja pronto para cada data comemorativa"

- **Arquivo:** `moti-datas`
- **Pose:** Moti abraçando ou segurando um calendário de mesa (folha com argolas em cima e um dia marcado com um círculo roxo), usando um chapéu de festa.
- **Expressão:** animado, com o braço livre comemorando.
- **Detalhes:** poucos confetes roxos e lilases caindo em volta, sem poluir.

## 3. Poses de outras telas

### Tarefas — "Mantenha o trabalho do time andando"

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

## 4. Modelo de pedido para uma pose nova

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
