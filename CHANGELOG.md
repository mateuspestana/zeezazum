# Changelog

Todas as mudanças notáveis deste projeto são documentadas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e o versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

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
