<div align="center">

  # Grabr
  *Baixador de vídeos e áudios self-hosted com interface web*

  [![Python](https://img.shields.io/badge/Python-3.8+-3776ab?style=flat-square)](https://python.org)
  [![Powered by yt-dlp](https://img.shields.io/badge/powered%20by-yt--dlp-ff0000?style=flat-square)](https://github.com/yt-dlp/yt-dlp)
  [![Licença](https://img.shields.io/badge/licença-MIT-blue?style=flat-square)](LICENSE)

  [Funcionalidades](#funcionalidades) • [Primeiros passos](#primeiros-passos) • [Como usar](#como-usar) • [API](#api)

</div>

---

grabr é um app Flask de arquivo único que envolve o [yt-dlp](https://github.com/yt-dlp/yt-dlp) em uma interface web limpa. Cole uma URL, escolha o formato, e a mídia é salva em uma pasta local — sem nuvem, sem conta, sem rastreamento e de graça.

## Funcionalidades

- Download de vídeo em MP4 (360p / 720p / 1080p / melhor disponível) ou extração de áudio em MP3
- Bitrate de MP3 configurável: 128 / 192 / 256 / 320 kbps
- Suporte completo a playlists com numeração automática dos arquivos
- Fila de downloads em tempo real com barra de progresso e cancelamento por tarefa
- Biblioteca de mídia integrada: pré-visualização de thumbnails, reprodução no navegador, busca e ordenação
- Funciona em Linux, macOS, Windows e **Termux (Android)** **(Foi testado somente no Windows e no Termux)**
- Zero configuração — distribuído como um único arquivo `.py`

## Primeiros passos

### Requisitos

- Python 3.8+
- [Flask](https://flask.palletsprojects.com/) e [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FFmpeg](https://ffmpeg.org/) *(opcional — necessário para extração de MP3 e mesclagem de vídeos adaptativos)*

### Instalação e execução

```bash
pip install flask yt-dlp
python grabr.py
```

Depois abra [http://localhost:5000](http://localhost:5000) no navegador.

> [!TIP]
> Para acessar o grabr de outro dispositivo na mesma rede local (ex: seu celular), acesse `http://<ip-da-sua-máquina>:5000`.

### Termux (Android)

O grabr detecta automaticamente o Termux pelas variáveis de ambiente `TERMUX_VERSION`/`PREFIX` e salva os arquivos na pasta Downloads compartilhada.

```bash
pkg install python ffmpeg
pip install flask yt-dlp
python grabr.py
```

## Como usar

### Baixando mídia

1. Cole a URL de um vídeo do YouTube (ou qualquer site suportado pelo yt-dlp) no campo de entrada.
2. Escolha **Vídeo** (MP4) ou **Áudio** (MP3) e a qualidade desejada.
3. Clique em **Baixar** — a tarefa aparece na fila com barra de progresso em tempo real.
4. Ao concluir, o arquivo aparece na aba **Arquivos**, onde você pode reproduzir, baixar ou excluir.

### Pasta de destino

Os arquivos são salvos em uma pasta criada automaticamente na inicialização:

| Plataforma | Caminho padrão |
|---|---|
| Linux / macOS / Windows | `~/Downloads/YT_DOWNLOADER_MEDIA` |
| Termux (Android) | `~/storage/shared/Download/YT_DOWNLOADER_MEDIA` |

Para salvar em outro lugar, defina a variável de ambiente `YT_DOWNLOAD_DIR`:

```bash
YT_DOWNLOAD_DIR=/data/media python grabr.py
```

## API

O grabr expõe uma pequena API REST para scripts e integrações.

| Método | Endpoint | Descrição |
|---|---|---|
| `POST` | `/api/info` | Busca título, thumbnail e duração sem baixar |
| `POST` | `/api/download` | Inicia uma tarefa de download em segundo plano |
| `POST` | `/api/cancel/<task_id>` | Cancela uma tarefa em andamento |
| `GET` | `/api/status` | Consulta o status de todas as tarefas ativas |
| `GET` | `/api/files` | Lista os arquivos baixados |
| `GET` | `/api/file/<filename>` | Transmite um arquivo baixado |
| `GET` | `/api/thumbnail/<filename>` | Serve a thumbnail de um arquivo |
| `DELETE` | `/api/delete/<filename>` | Exclui um arquivo e sua thumbnail |

**Exemplo — iniciar um download:**

```bash
curl -X POST http://localhost:5000/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtu.be/dQw4w9WgXcQ", "type": "mp3", "audio_bitrate": "320"}'
```

> [!NOTE]
> O grabr foi pensado para uso pessoal e local. Não é recomendado expô-lo em redes públicas ou não confiáveis sem alguma camada adicional de autenticação.
