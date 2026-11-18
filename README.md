# 🎬 Clipador de lives — Twitch, Kick, YouTube

O robô assiste a uma ou mais lives ao mesmo tempo e faz sozinho o seguinte:

1. **Grava desde o 1º segundo** com o `streamlink`. Ele espera a live começar e reconecta sozinho se ela cair.
2. **Lê o chat** e marca um pico quando as mensagens explodem. Ele ignora bots, comandos (`!`) e spam, e dá peso maior para risadas e hype.
3. **Plano B pelo áudio:** se o chat não conectar, detecta gritos e reações do streamer pelo volume.
4. **Acha o momento exato** pelo pico de volume perto da reação do chat.
5. **Corta em pausas da fala**, sem cortar no meio de uma palavra, respeitando a duração exigida por cada rede.
6. **Monta o vídeo vertical 1080x1920** com legenda estilo shorts (Whisper), título, crédito do canal e volume em -14 LUFS (o padrão das redes).
7. **Entrega o texto do post** pronto (`.txt` com hashtags) e um `ranking.csv` com os melhores clipes primeiro.

## Começo rápido (GTA 6)

```bash
./gta6.sh https://twitch.tv/CANAL1 https://twitch.tv/CANAL2 https://kick.com/CANAL3
```

Esse atalho já usa o perfil `gta6`, que inclui:

- título `NOMEDOCANAL NO GTA 6`;
- hashtags `#gta6 #gtavi ...`;
- vocabulário do Whisper com os nomes do jogo (Vice City, Leonida, Lucia, Jason...).

Os clipes vão para `saida/<plataforma>_<canal>/clipes/`.

Antes de começar, rode `python3 -m clipador quadro URL` para ver uma prévia de como o vídeo vai ficar.

## Formatos e regras das redes (pesquisadas em nov/2026)

| Formato (`--formatos`) | Duração | Para quê |
|---|---|---|
| `tiktok` (padrão) | **63 a 90 s** | O TikTok Creator Rewards só paga vídeos com **mais de 1 minuto**. O mesmo arquivo serve para o **Reels**, que recomenda para não seguidores vídeos de até 3 min. |
| `shorts` | 25 a 58 s | No YouTube Shorts, vídeo com mais de 1 min e conteúdo reivindicado (Content ID) fica **sem monetização**. |
| `curto` | 15 a 35 s | Teasers e clipes de engajamento. |

Para gerar mais de um formato: `--formatos tiktok,shorts`. Cada formato a mais adiciona cerca de 25 s de processamento por clipe.

Cada clipe gera estes arquivos:

- `.mp4` — o vídeo pronto;
- `.txt` — título, frase, crédito e hashtags para colar no post;
- `.srt` — a legenda separada;
- `.json` — score, mensagens do chat e transcrição completa.

## Layouts

| `--layout` | Visual |
|---|---|
| `desfoque` (padrão) | A tela inteira da live com zoom leve sobre um fundo desfocado, título em cima e legenda embaixo. Funciona com qualquer streamer. |
| `camera` | Webcam em cima e gameplay embaixo, o formato clássico de clipe. Precisa de `--camera x,y,largura,altura` em % da tela. |
| `cheio` | Recorte 9:16 do centro da tela (gameplay em tela cheia). |
| `horizontal` | 16:9 para YouTube normal e X. |

Para descobrir o `--camera` de um streamer:

```bash
python3 -m clipador quadro https://twitch.tv/CANAL --layout camera --camera 0,62,27,38
```

O comando gera em `saida/previas/` um print da live e uma prévia do layout. Ajuste os números até a webcam ficar certinha.

## Opções úteis

| Opção | Padrão | O que faz |
|---|---|---|
| `--sensibilidade` | 2.5 | Quantas vezes acima do normal o chat precisa ficar. **Menor = mais clipes.** |
| `--min-msgs` | 15 | Mínimo de mensagens em 10 s. Para canal pequeno, use 5 a 8. |
| `--intervalo` | 60 | Tempo mínimo entre dois clipes do mesmo canal. |
| `--sinal` | auto | `auto` usa o chat e cai para o áudio se o chat não funcionar. Também aceita `chat`, `audio` ou `ambos`. |
| `--gancho` | – | Abre o vídeo com 3,5 s do momento principal antes do contexto, o que prende quem está rolando o feed. ⚠️ No Shorts, violência gráfica nos primeiros 7 s perde anúncios. |
| `--titulo` | do perfil | Texto fixo no topo. `{CANAL}` vira o nome do canal. Use `--titulo ""` para não ter título. |
| `--fps` | 30 | Use 60 para gameplay mais fluido. Processa cerca de 2x mais devagar. |
| `--modelo` | large-v3-turbo | Modelo do Whisper. `small` é mais rápido e erra mais. |
| `--idioma` | pt | `en`, `es`... ou `auto`. |
| `--manter-minutos` | 45 | A gravação mais velha que isso é apagada para não lotar o disco. |
| `--gravar-tudo` | – | Guarda a live inteira, que vira um mp4 no final. Ocupa cerca de 3 GB por hora em 1080p60. |
| `--kick-chatroom-id` | – | Normalmente desnecessário: o robô descobre a sala e a chave atual do chat da Kick sozinho. Use só se a Kick bloquear a busca (o número está em `chatroom.id` de `kick.com/api/v2/channels/CANAL`). |
| `--duracao` | 0 | Para sozinho depois de N segundos, bom para testes. |

## Outros comandos

```bash
python3 -m clipador chat https://twitch.tv/CANAL
python3 -m clipador cortar live.mp4 --inicio 01:23:10 --fim 01:24:20 --perfil gta6 --canal twitch.tv/CANAL
```

O `chat` só mede o chat, para calibrar a sensibilidade. O `cortar` faz um corte manual exato.

## ⚠️ Antes de postar: monetização e direitos

- **Conteúdo não original:** TikTok, Meta (Reels/Facebook) e YouTube reduzem alcance e **cortam a monetização** de quem só reposta clipe dos outros. Contam a favor:
  - legenda, título e layout próprios (o robô já faz);
  - **sua narração ou comentário**, ou uma edição com contexto (isso você adiciona: é o que mais pesa);
  - nunca repostar com marca d'água de outra rede.
- **Permissão do streamer:** o clipe é conteúdo do streamer. Muitos têm programa oficial de clipadores. Prefira esses canais ou peça autorização. O crédito no vídeo já vem ligado.
- **Rockstar e spoilers:** a Rockstar derruba vídeos **isolados de cutscenes e do final do jogo**. Gameplay e reações, tudo bem. Revise clipes em que a tela está numa cutscene de história.
- **Shorts:** violência gráfica nos primeiros 7 s pode perder anúncios. Por isso o `--gancho` vem desligado.

## Desempenho

Numa máquina de 4 núcleos sem GPU, cada clipe leva cerca de 70 s depois do pico (Whisper cerca de 30 s e vídeo cerca de 30 s). A fila processa primeiro os picos mais fortes e descarta picos que ficaram velhos demais. Com 3 ou mais canais muito agitados, use `--sensibilidade 3`. Com GPU NVIDIA, o Whisper usa a GPU sozinho e fica cerca de 10x mais rápido.

## Usando no Claude Code na nuvem

O ambiente da nuvem já instala o streamlink, o ffmpeg, o faster-whisper e o modelo do Whisper sozinho. É só abrir uma sessão com este repositório e pedir, por exemplo: "liga o clipador nos canais X e Y".

## Instalação em outra máquina (VPS, PC, WSL)

```bash
./install.sh
```

Precisa de Ubuntu/Debian ou WSL e Python 3.10+.
