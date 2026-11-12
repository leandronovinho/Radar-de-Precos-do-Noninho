# Radar de preços

Lê canais públicos de promoção do Telegram, guarda cada oferta (produto, preço, cupom, loja, hora)
e mostra o histórico de preços em um site com busca e gráfico. Tudo gratuito, rodando no GitHub.

## O que tem aqui

- `canais.txt`: a lista de canais que o coletor lê. Edite este arquivo.
- `coletor.py`: lê os canais, guarda cada post em `dados/posts/` (um arquivo por mês) e monta `dados/indice.json`,
  que é o que o site mostra. Como os textos ficam guardados, uma melhoria no coletor reprocessa todo o histórico.
- `index.html`: o site (categorias, busca, gráfico por dia, preço-alvo, comparação). Sem dados, mostra um exemplo.
- `.github/workflows/coletar.yml`: faz o GitHub rodar o coletor a cada 15 minutos.

## Como colocar no ar

1. Crie uma conta em github.com e um repositório novo, **público** (ex.: `radar-de-precos`).
2. Envie todos os arquivos desta pasta para o repositório, mantendo as pastas
   (inclusive a `.github`, que fica oculta em alguns celulares e computadores).
3. Edite `canais.txt` com os canais que você quer. Use o nome que vem depois de `t.me/`.
4. Em **Settings > Actions > General > Workflow permissions**, marque **Read and write permissions**.
5. Em **Actions**, abra **Coletar ofertas** e toque em **Run workflow** para a primeira coleta.
6. Em **Settings > Pages**, escolha **Deploy from a branch**, branch `main`, pasta `/ (root)`.
   O site fica em `https://SEU-USUARIO.github.io/radar-de-precos/`.

## Limites que você precisa conhecer

- Só funciona com **canais públicos**. Para testar um canal, abra `https://t.me/s/NOME` no navegador:
  se os posts aparecerem, o coletor consegue ler. Grupos e canais privados não aparecem ali.
- A página pública mostra só os posts mais recentes (cerca de 20). Rodando a cada 15 minutos isso
  basta, mas em dias de muito movimento algum post pode escapar.
- O GitHub às vezes atrasa a execução agendada em alguns minutos.
- O gráfico mostra os preços **de oferta** publicados nos canais, a partir da primeira coleta.
- Placas de vídeo e processadores são agrupados pelo modelo (ex.: RX 9060 XT 16GB, qualquer marca).
  Os outros produtos são agrupados pela semelhança do nome, então o mesmo item escrito de jeitos
  muito diferentes ainda pode aparecer duas vezes.
- O preço-alvo é o preço que ficou entre os 25% mais baratos do histórico e só aparece depois de 5 dias com oferta.
