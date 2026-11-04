# Changelog

Todas as mudanças notáveis deste projeto são documentadas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e o versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

## [0.3.0] — 2026-11-04

O Zeezazum passa a coletar também o TikTok, sem login.

### Adicionado
- `platform: tiktok` no `config.yaml` e `prepare --platform tiktok` (gera `tiktok_url`).
  Reconhece `tiktok.com/@handle`, links de vídeo e rótulos "TikTok: @handle"; links
  curtos são ignorados com aviso no log.
- Coleta pelo yt-dlp, porque o navegador anônimo recebe a lista de vídeos vazia do
  TikTok. O navegador só lê os dados do perfil: classifica perfil inexistente ou
  privado sem gastar tentativas e fornece o total de vídeos para o aviso de cobertura
  do `backfill --all`.
- Vídeo (maior qualidade, configurável em `media.tiktok_video_format`) e capa de
  cada post. Posts de fotos guardam só a capa.
- Métricas do TikTok: curtidas, comentários, visualizações, compartilhamentos,
  salvamentos, duração, hashtags, menções, trilha e autor.
- Retry com espera crescente na abertura do perfil (o TikTok é intermitente) e
  retomada da listagem sem duplicar posts quando a paginação falha no meio. Lista
  incompleta nunca é devolvida como completa.
- Dependência `yt-dlp` (>= 2026.10.26) e testes sem rede para o TikTok, com fixture
  de entradas reais; teste ao vivo opcional com `ZEEZAZUM_LIVE=1`.

### Alterado
- O estado e as tarefas de mídia passam a ser separados por plataforma em todas as
  operações; o mesmo @ no Instagram e no TikTok não se mistura. Os dados do
  Instagram não mudam de lugar.
- O downloader saiu de `platforms/instagram/` para `platforms/downloader.py`.
- O `.gitignore` agora cobre `.venv` como symlink e `.venv.nosync`.

### Limitações conhecidas
- Em um perfil com 1516 vídeos declarados, a listagem completa trouxe 1479.
- Perfil privado não foi testado contra uma conta privada real.
- A detecção de slideshows se baseia no código do yt-dlp, sem verificação ao vivo.
- Se o TikTok mudar a API, o yt-dlp precisa ser atualizado.

## [0.2.5] — 2026-11-04

### Adicionado
- `zeezazum backfill --all`: baixa o histórico completo de cada perfil. Ignora
  `max_posts`, `since_date` e o early-stop, e só encerra após 25 rodadas de scroll
  sem posts novos (antes eram 8).
- Aviso no log quando o total salvo fica abaixo do número de posts declarado no
  perfil; rodar `backfill --all` de novo continua de onde parou (dedup por shortcode).
- Teste do modo `fetch_all`.

## [0.2.4] — 2026-11-03

Sincronização com o [Zeeschuimer](https://github.com/digitalmethodsinitiative/zeeschuimer)
(v1.14.3 e commits até 2026-10-21, módulo `instagram.js`).

### Corrigido
- Fallback genérico do parser passa a descartar nós de outros perfis (conteúdo
  pré-buscado pelo Instagram no mesmo endpoint), mantendo posts em coautoria com
  o perfil raspado. O número de nós descartados vai para o log.
- Respostas de `explore_grid`, `injected_story_units` e `logging_client_events`
  são ignoradas, como no Zeeschuimer.

### Adicionado
- Suporte ao shape Polaris: `display_uri` como fonte de imagem e tipo de mídia
  derivado de `__typename` quando `media_type` não vem no nó.
- Testes do parser com nós sintéticos.

### Não portado
- Regex de hashtag do 4CAT (exclui `_` e corta `#foo_bar`), prefixo `for (;;);`,
  allowlist de views e novas views (`user_tagged`, `user_reposts`, `user_reels`).

## [0.2.3] — 2026-11-03

Esta versão sucede diretamente a 0.2.1; não houve release 0.2.2.

### Corrigido
- Estado independente por identificador, plataforma e handle, com migração
  transacional e backup do banco antigo.
- Downloads persistidos no DuckDB e retomados automaticamente; falhas de rede ou
  gravação não são tratadas como sucesso. Contagem de pendências reconciliada.
- URLs de mídia reencontradas são atualizadas sem substituir métricas históricas.
- Checkpoints parciais não avançam o marcador incremental antes do fim do perfil.
- Sessão expirada/verificação obrigatória interrompe novos perfis e retorna código 3.
- Arquivos de posts ilegíveis não são sobrescritos silenciosamente.

### Adicionado
- Workers e fila de download limitados; resumo de resultados e código 1 para
  downloads que permanecem com falha.
- Checkpoints consistentes do estado no S3, configuráveis por
  `storage.s3.state_sync_interval_seconds` (padrão: 60 segundos).
- Preservação do estado temporário quando o upload final falha.
- Testes automatizados de recuperação, migração e autenticação.

### Documentação
- README reorganizado com início rápido, comandos, referência de configuração,
  operação no servidor, códigos de saída, migração e procedimentos de recuperação.
- Limites da coleta incremental, URLs expiradas e execução única por destino.

## [0.2.1] — 2026-10-16

### Adicionado
- Métricas por post em `posts.json`/`posts.parquet`: `like_count`, `comment_count`,
  `play_count`, `view_count`, `media_type`, `product_type`, `video_duration`,
  localização, `usertags`, coautores, áudio, `hashtags`, `mentions`, entre outras
  (campos ausentes viram nulos). Cada post também recebe `collected_at` (o
  engajamento é uma fotografia do momento da coleta) e `is_pinned`.
- `Post` ganha os campos opcionais `metrics` e `is_pinned`; novo
  `extract_metrics()` no parser do Instagram.
- `profile.json` por perfil, com seguidores, seguindo e total de posts (lidos de
  `og:description`) e quantos posts foram raspados — base para medir cobertura.
- JSON bruto de cada post novo guardado em chunks `raw/<timestamp>.jsonl.gz`,
  sem inchar o `posts.json`.
- Checkpoints durante o scroll (a cada 200 posts, parâmetro `on_checkpoint`):
  persistem posts e enfileiram mídia sem esperar o fim do perfil.
- Comando `zeezazum media-sync`: baixa a mídia que falta em disco para posts já
  raspados, sem abrir navegador (recupera downloads interrompidos).

### Corrigido
- Scroll não rolava a grade: o mouse ficava em (0,0), onde o `wheel` cai na
  barra lateral fixa do Instagram (causa do "só 3 posts por perfil"). Agora o
  mouse é movido para o centro da página antes de cada scroll.
- Respostas da API do Instagram eram descartadas por um filtro de `content-type`
  que exigia "json"; o filtro foi removido.
- Posts fixados no topo da grade não contam mais para o corte `since_date`: são
  necessários 12 posts não fixados mais antigos para encerrar o scroll.

### Alterado
- `_MAX_STAGNANT_SCROLLS` sobe de 4 para 8 e há pausas longas periódicas no
  scroll, para perfis longos e menor risco de bloqueio.
- Persistência idempotente por `shortcode` (checkpoint e fim do perfil não
  duplicam posts nem mídia); `pl.DataFrame` usa `infer_schema_length=None`.
- Viewport do navegador fixado em 1280x900.
- Downloader pula mídia que já existe; `LocalStorage.write_bytes` é atômico
  (escreve em `.part` e renomeia).
- Logger `httpx` em WARNING.

## [0.2.0] — 2026-09-24

### Corrigido
- O estado por perfil (`_state.duckdb`, usado para resume e raspagem
  incremental) agora é sincronizado com o backend remoto quando
  `storage.backend: s3` — antes ficava sempre preso ao disco local, mesmo com
  posts/mídia indo pro bucket. Com S3, o estado é baixado no início da
  execução (se já existir) e enviado de volta ao final, então ele viaja com os
  dados e não fica preso a uma instância EC2 específica.

### Documentação
- Seção completa no `README.md` sobre como configurar `storage.backend: s3`:
  criação do bucket, formas de fornecer credenciais AWS (IAM role no EC2,
  variáveis de ambiente, `aws configure`), policy IAM mínima necessária
  (`s3:PutObject` + `s3:GetObject` restrito ao prefixo), e como validar que
  está funcionando antes de rodar em escala.

## [0.1.0] — 2026-09-24

Primeira versão pública.

### Adicionado
- Scraper de Instagram via Playwright, capturando o tráfego de rede da própria
  página (mesma filosofia do Zeeschuimer) em vez de fazer scraping de DOM.
- Comando `zeezazum prepare`: extrai contas de uma plataforma (Instagram) de
  uma coluna de texto livre com redes sociais, com parsing tolerante a URLs
  sujas, prefixos, maiúsculas e handles sem protocolo; suporta
  `--extra-columns` para levar junto colunas de identificação da tabela de
  origem.
- Comando `zeezazum scrape`: raspagem incremental "pra frente no tempo" — para
  assim que reencontra o post mais recente já conhecido, segura para rodar
  repetidamente via cron.
- Comando `zeezazum backfill`: aprofunda o histórico de contas já raspadas,
  buscando posts mais antigos sem re-baixar mídia já salva.
- Download de posts e mídia (imagens e vídeos) em duas fases desacopladas:
  extração de posts (rápida, sequencial por conta) e download de mídia
  (assíncrono, em fila, rodando em paralelo à raspagem do próximo perfil).
- Estado por perfil em DuckDB (`_state.duckdb`) para resume de execuções
  interrompidas e raspagem incremental.
- Storage intercambiável (`local` ou `s3`) via `config.yaml`.
- Fluxo de login desacoplado do servidor: sessão gerada localmente
  (`scripts/local_login.py`) e copiada para o EC2 via `storage_state.json`.
- Documentação de deploy passo a passo no EC2 no `README.md`.
