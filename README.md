# Zeezazum

[![DOI](https://zenodo.org/badge/1385550085.svg)](https://doi.org/10.5281/zenodo.23932959)
[![versão](https://img.shields.io/github/v/release/mateuspestana/zeezazum?label=versão)](https://github.com/mateuspestana/zeezazum/releases)
[![licença](https://img.shields.io/github/license/mateuspestana/zeezazum?label=licença)](LICENSE)
[![atualizado](https://img.shields.io/github/last-commit/mateuspestana/zeezazum?label=atualizado)](https://github.com/mateuspestana/zeezazum/commits/main)

Coleta posts, imagens e vídeos do Instagram e do TikTok a partir de tabelas de
contas. No Instagram, usa Playwright para capturar as respostas de rede enquanto
navega pelo perfil, inspirado no
[Zeeschuimer](https://github.com/digitalmethodsinitiative/zeeschuimer). O TikTok
funciona sem login e usa o yt-dlp (veja [TikTok](#tiktok)).
Não depende de um dataset específico: qualquer tabela com identificador e texto
contendo URLs ou handles pode alimentar a coleta.

**Versão: 0.3.3** · [Histórico de mudanças](CHANGELOG.md)

**Autor:** Matheus C. Pestana ([matheus.pestana@iesp.uerj.br](mailto:matheus.pestana@iesp.uerj.br)).

## Início rápido

Requer Python 3.11 ou superior e [uv](https://docs.astral.sh/uv/).
Execute na raiz do projeto:

```bash
uv sync
uv run playwright install chromium
cp config.example.yaml config.yaml
```

Prepare as contas a partir de CSV, Parquet ou XLSX:

```bash
uv run zeezazum prepare \
  --input data/candidatos_atual.parquet \
  --column "REDES SOCIAIS" --id-column SQ_CANDIDATO \
  --extra-columns "NM_URNA_CANDIDATO,SG_UF,SG_PARTIDO,DS_CARGO" \
  --output data/candidatos_instagram.parquet
```

O resultado contém `row_id`, `handle` e `instagram_url`, além das colunas extras
opcionais. Cada conta encontrada vira uma linha; uma pessoa pode ter várias contas.
O parser aceita URLs misturadas com outras redes e texto livre.

Faça o login **na máquina com interface gráfica**:

```bash
uv run python scripts/local_login.py
```

Entre manualmente no Instagram e pressione Enter no terminal. A sessão fica em
`sessions/instagram_storage_state.json`. Esse arquivo dá acesso à sessão da conta:
não o inclua em commits nem o compartilhe publicamente.

No `config.yaml`, ajuste `input.path`, os nomes das colunas, o limite de posts e o
destino dos arquivos. Comece com uma tabela de 2–5 contas:

```bash
uv run zeezazum scrape --config config.yaml
```

Confira os arquivos de saída e `logs/zeezazum.log` antes de ampliar o lote.

## TikTok

O TikTok não pede login. Prepare a tabela com `--platform tiktok` e aponte o
`config.yaml` para ela com `platform: tiktok`:

```bash
uv run zeezazum prepare \
  --input data/candidatos_atual.parquet \
  --column "REDES SOCIAIS" --id-column SQ_CANDIDATO \
  --platform tiktok --output data/candidatos_tiktok.parquet
uv run zeezazum scrape --config config.yaml
```

O resultado traz `row_id`, `handle` e `tiktok_url`. O parser reconhece
`tiktok.com/@handle`, links de vídeo e rótulos como "TikTok: @handle". Links curtos
(`vm.tiktok.com/...`) só se resolvem com uma requisição, então são ignorados e o log
avisa quais foram. Cada execução cuida de uma plataforma. Para coletar as duas, use
dois `config.yaml`; o estado e as pastas de saída ficam separados por plataforma.

Por baixo, o navegador anônimo recebe uma lista de vídeos vazia do TikTok, e por isso
a listagem e o download dos vídeos são feitos pelo yt-dlp. Testamos o Playwright com
stealth (`playwright-stealth`), no Chromium embutido e no Chrome real, com e sem
janela: o TikTok mostra um captcha de deslizar o quebra-cabeça e a lista continua
vazia. Não automatizamos a resolução do captcha. O navegador entra só para
ler os dados do perfil: se ele existe, se é privado e quantos vídeos declara ter.
Perfil inexistente vira erro na hora, sem gastar tentativas (testado: o TikTok
devolve `statusCode 10221`). O perfil privado deveria ter o mesmo tratamento, mas não
foi testado contra uma conta privada real. O total declarado de vídeos alimenta o
aviso de cobertura do `backfill --all`.

### Quando o TikTok bloqueia: resolver o captcha à mão

Se a listagem de um perfil esgotar as tentativas com o captcha na tela, o `scrape`
para com o código de saída 3 e manda você resolvê-lo uma vez. Numa máquina com
interface gráfica:

```bash
uv run zeezazum tiktok-session
```

Abre uma janela visível no TikTok; deslize o quebra-cabeça até encaixar, espere os
vídeos do perfil aparecerem e aperte Enter no terminal (sem terminal interativo, como
um `!comando` no Claude Code, ele salva sozinho quando o captcha some, em até 5 minutos). A sessão (cookies) fica em
`sessions/tiktok_storage_state.json` (`scraping.tiktok_session_file`), é usada pelo
navegador e pelo yt-dlp nas próximas execuções, e dá acesso à sessão do navegador: não
a versione. Depois, repita o comando que parou. Perfis já coletados não são refeitos.
Num servidor sem tela, gere a sessão no seu computador e copie o arquivo com `scp`.

Para tags, a sessão foi verificada ao vivo (veja abaixo). Para perfis, não se sabe se ela
reduz os bloqueios do yt-dlp: os testes cobrem a conversão dos cookies, a detecção e o
fluxo do comando, mas essa comparação não foi medida. O captcha aparece em quase toda abertura anônima do
perfil (6 de 6 nos testes), mesmo quando a coleta funciona; por isso o Zeezazum só pede
a resolução quando a listagem falha de fato. Se mesmo com a sessão o bloqueio voltar, o
aviso diz que a sessão não bastou, e a saída é esperar e repetir (os bloqueios do
TikTok também passam sozinhos).

### Hashtags do TikTok (`tiktok_tag`)

Para coletar os vídeos de hashtags, use `platform: tiktok_tag`. Cada tag vira
`tiktok.com/tag/<nome>`; a tabela de entrada pode ter `#bolsonaro`, `bolsonaro` ou o
link, separados por espaço, vírgula, ponto e vírgula ou quebra de linha:

```bash
uv run zeezazum prepare --input data/tags.csv --column tags --id-column id \
  --platform tiktok_tag --output data/tags_tiktok.parquet   # gera tiktok_tag_url
```

Os vídeos ficam em `output/tiktok_tag/<tag>/`, com o mesmo formato dos perfis
(`owner_username` diz quem postou). A tag é normalizada para minúsculas.

**A sessão com o captcha resolvido é obrigatória para tags.** Sem ela, o TikTok entrega
a página da tag mas devolve a lista de vídeos vazia e mostra o captcha (5 de 5 aberturas
anônimas em novembro de 2026), e o extrator de tags do yt-dlp está quebrado. Com a
sessão, a coleta funcionou ao vivo: 60 posts de `#bolsonaro` em headless, e o fluxo
completo (`prepare` + `scrape`, duas tags, 12 vídeos mp4 e 12 capas) levou 21 segundos.
O Zeezazum lê a lista como o Zeeschuimer: rola a página e captura
`/api/challenge/item_list/`. Crie a sessão uma vez:

```bash
uv run zeezazum tiktok-session --tag bolsonaro
```

A janela "Log in to TikTok" (e pedidos de idade) que aparece na página pode ser ignorada:
a coleta não interage com ela e não precisa de conta.

Sem a sessão, ou se ela não bastar, o `scrape` para com código 3 e manda você rodar o
comando acima. Também vale que: a lista de uma tag não é cronológica, então não existe
"parar no post já conhecido" nem corte antecipado por `since_date` (o filtro por data e
o `max_posts` valem no final, sobre o que foi capturado); a coleta termina em
`max_posts`, quando o TikTok avisa o fim da lista ou quando a página para de trazer
posts novos. **O TikTok limita a profundidade de cada tag**: medido em novembro de 2026,
`#bolsonaro` entregou 226 posts (de março de 2022 a novembro de 2026), `#lula` 207 e
`#eleicoes2026` 213, cada uma em cerca de 25 segundos e a API avisou `hasMore: false`
ali. Rolar mais não adianta, e `max_posts` acima disso não muda nada. Para ir além, rode
o `scrape` de novo de tempos em tempos: cada passada pega os posts mais recentes e o que
já está salvo não é refeito, então o acervo cresce com as execuções. Para o histórico
completo de quem posta na tag, colete os perfis dos autores (`platform: tiktok`); e os campos de posts de
foto (slideshow) vêm do formato da API web e não foram verificados ao vivo (nenhum slideshow
apareceu nos testes); o `profile.json` de uma tag traz só o nome e o total coletado.

### Buscas do TikTok (`tiktok_search`)

Para coletar os vídeos de uma busca, use `platform: tiktok_search`. A tabela de entrada
pode ter o texto da busca (uma por linha ou separadas por `;`) ou links como
`tiktok.com/search/video?q=jair+bolsonaro`, `tiktok.com/search?q=...` e
`tiktok.com/search/jair+bolsonaro`; todos viram a mesma busca:

```bash
uv run zeezazum prepare --input data/buscas.csv --column busca --id-column id \
  --platform tiktok_search --output data/buscas_tiktok.parquet  # gera tiktok_search_url
```

Os vídeos ficam em `output/tiktok_search/jair+bolsonaro/` (minúsculas, espaço vira `+`),
no mesmo formato dos perfis e das tags. Só a aba de vídeos é coletada. Como nas tags,
**é preciso a sessão do captcha resolvido**, e a busca tem o seu próprio captcha:

```bash
uv run zeezazum tiktok-session --tag bolsonaro --search "jair bolsonaro"
```

`--tag` e `--search` podem vir juntos: as páginas abrem em sequência, na mesma sessão.
Medido em novembro de 2026: `jair bolsonaro` entregou 149 posts em 34 segundos, e o
TikTok avisou o fim da lista; `lula` entregou 119. A lista de uma busca também não é
cronológica, então valem as mesmas regras das tags (sem parada no post já conhecido;
`since_date` e `max_posts` no final).

**A sessão não dura para sempre.** Depois de umas dez coletas e sondagens seguidas em
cerca de uma hora, o captcha voltou, inclusive nas tags, e não saiu em 5 minutos. Nesse
caso o `scrape` para com código 3 e pede para rodar `tiktok-session` de novo. Não se sabe
se é limite de ritmo ou expiração; para lotes grandes, comece com poucas buscas e tags e
espace as coletas. O `tiktok-session` só grava a sessão quando o captcha sumiu **e** há
vídeos na página, e não sobrescreve a sessão existente se falhar: não feche o modal pelo X.

O que esperar dessa via:

- **É intermitente.** Nos testes de novembro de 2026, só cerca de 40% das aberturas
  de perfil deram certo de primeira, e o TikTok respondeu 429 algumas vezes. O
  Zeezazum repete com espera crescente (até 10 tentativas por passada) e, se a
  paginação falha no meio, recomeça pulando o que já viu. Uma lista incompleta nunca
  é entregue como se fosse completa: se as tentativas acabam, o perfil fica com
  status de erro e a próxima execução tenta de novo.
- **O tempo varia muito.** Com a abertura do perfil dando certo de primeira, três
  posts com vídeo saíram em 14 segundos; numa rodada anterior, a mesma coleta levou
  quase quatro minutos até o TikTok deixar passar. Para uma lista grande de contas,
  comece com um piloto de umas cinco e meça antes de soltar o lote.
- **A contagem pode não bater.** Num perfil com 1516 vídeos declarados, a listagem
  completa trouxe 1479. Pode ser conteúdo que o TikTok não lista publicamente; não
  há como distinguir de perda, então trate o `profile.json` como referência, não
  como garantia.
- **Vídeos.** O arquivo salvo é o de maior qualidade, que costuma ser HEVC. Se
  precisar de H.264 (mais compatível com navegadores), use
  `media.tiktok_video_format: "best[vcodec^=h264]/best"`.
- **Capas.** São baixadas junto, mas a URL expira em cerca de dois dias; depois
  disso, o `media-sync` não recupera a capa de posts antigos.
- **Posts de fotos (slideshows)** guardam só a capa: o yt-dlp não baixa o carrossel.
  A identificação desses posts vem da leitura do código do yt-dlp e não foi
  verificada contra um slideshow real.
- **Sem fixados.** A listagem não diz quais posts estão fixados, então o corte por
  `since_date` considera todos. Fixados antigos no topo não atrapalham o marcador
  incremental, porque os posts são ordenados por data antes de gravar.
- As métricas do TikTok são `like_count`, `comment_count`, `view_count`,
  `share_count`, `save_count`, `video_duration`, `hashtags`, `mentions`,
  `audio_title`, `audio_artist`, `owner_username` e `owner_id`.

O yt-dlp acompanha as mudanças do TikTok. Se as coletas começarem a falhar de
repente, o primeiro passo é `uv lock --upgrade-package yt-dlp`.

## Qual comando usar

| Comando | Quando usar | Abre navegador? |
| --- | --- | --- |
| `prepare` | Extrair contas de uma tabela de origem | Não |
| `scrape` | Primeira coleta ou atualização de posts novos | Sim |
| `backfill --extra-posts N` | Aprofundar o histórico já coletado | Sim |
| `media-sync` | Recuperar imagens/vídeos ausentes de posts salvos | Não |

Todos os comandos de coleta aceitam `--config`, com padrão `config.yaml`.
Use `uv run zeezazum COMANDO --help` para consultar as opções.

```bash
uv run zeezazum scrape --config config.yaml
uv run zeezazum backfill --config config.yaml --extra-posts 20
uv run zeezazum backfill --config config.yaml --all   # histórico completo
uv run zeezazum media-sync --config config.yaml
```

### Incremental, histórico e retomada

`scrape` para ao reencontrar o post mais recente conhecido. Aumentar `max_posts`
não aprofunda automaticamente uma conta já coletada: use `backfill` para isso.
O backfill busca até o total já salvo mais `extra-posts`; contas ainda sem estado
usam o `max_posts` configurado. O corte `since_date` também se aplica ao backfill.

Para baixar **todo** o histórico, use `backfill --all`: ignora `max_posts`, `since_date`
e o early-stop, e só encerra quando o Instagram para de entregar posts (25 rodadas de
scroll sem novidade). Ao final, o log avisa se o total salvo ficou abaixo do número de
posts declarado no perfil; rodar o comando de novo continua de onde parou, pois os
posts são deduplicados. Perfis grandes levam horas: o scroll tem pausas longas de
propósito, e a sessão pode expirar (código 3).

Posts são deduplicados por shortcode. Durante scrolls longos há checkpoints a
cada 200 posts capturados; o marcador incremental só avança ao concluir o perfil.
Uma interrupção pode repetir a navegação, mas os posts salvos são reaproveitados.
A coleta depende do conteúdo que o Instagram entrega e não garante um arquivo
completo do perfil. Remoções, mudanças na ordenação e limitações de acesso podem
afetar a cobertura.

Antes de coletar novos posts, `scrape` e `backfill` também verificam mídias ausentes
para as contas do input. `media-sync` faz somente essa recuperação. Tarefas ficam
registradas no DuckDB; arquivos já existentes são reaproveitados. Sucesso só é
registrado depois de gravar o arquivo. Itens desabilitados na configuração não
entram na contagem de pendências daquela execução.

## Configuração

[config.example.yaml](config.example.yaml) contém a configuração completa.

| Campo | Padrão | Efeito |
| --- | --- | --- |
| `input.path` | Obrigatório | Tabela com as contas |
| `input.id_column` / `url_column` | Obrigatórios | Identificador e texto/URL da conta |
| `scraping.browser` | `chromium` | Também aceita `firefox`, após instalar seu browser Playwright |
| `scraping.max_posts` | `60` | Limite por perfil; `null` remove esse limite |
| `scraping.since_date` | `null` | Corte de data, por exemplo `"2024-01-01"` |
| `scraping.headless` | `true` | Navegador sem janela |
| `platform` | `instagram` | `instagram` ou `tiktok`; uma plataforma por execução |
| `scraping.session_file` | `sessions/instagram_storage_state.json` | Sessão criada localmente (Instagram) |
| `scraping.tiktok_session_file` | `sessions/tiktok_storage_state.json` | Sessão do TikTok com o captcha resolvido à mão (opcional, veja [TikTok](#tiktok)) |
| `scraping.delay_between_profiles_seconds` | `[4, 10]` | Espera aleatória entre perfis |
| `scraping.retry.max_attempts` | `3` | Tentativas para erros gerais de extração |
| `scraping.retry.backoff_seconds` | `30` | Espera entre essas tentativas |
| `storage.backend` | `local` | `local` ou `s3` |
| `storage.local.base_path` | `output` | Diretório de saída local |
| `storage.s3.bucket` | `null` | Obrigatório ao usar S3 |
| `storage.s3.prefix` | `zeezazum/` | Prefixo dentro do bucket |
| `storage.s3.region` | `us-east-1` | Região AWS |
| `storage.s3.state_sync_interval_seconds` | `60` | Intervalo positivo entre checkpoints do estado no S3 |
| `media.download_images` / `download_videos` | `true` | Tipos de mídia habilitados |
| `media.max_concurrent_downloads` | `4` | Número positivo de workers de download |
| `media.tiktok_video_format` | `best` | Formato do yt-dlp para vídeos do TikTok |
| `logging.level` / `file` | `INFO` / `logs/zeezazum.log` | Registro detalhado da execução |

Perfis são visitados sequencialmente usando uma sessão. Apenas downloads de mídia
rodam em paralelo, com fila limitada. Cada download tem até três tentativas com
espera exponencial e timeout HTTP de 60 segundos.

## Arquivos produzidos

```text
output/
├── _state.duckdb
└── instagram/<handle>/        (ou tiktok/<handle>/)
    ├── posts.json
    ├── posts.parquet
    ├── profile.json
    ├── raw/<timestamp>.jsonl.gz
    └── media/<shortcode>_<indice>.jpg|png|mp4
```

- `posts.json` e `posts.parquet`: conteúdo, timestamps, legenda, mídia e métricas
  disponíveis. Campos ausentes podem ser nulos. `collected_at` identifica o momento
  da fotografia de engajamento; atualizar a coleta não renova todas as métricas antigas.
- `profile.json`: estatísticas disponíveis na página, possivelmente arredondadas,
  e número de posts persistidos. Não representa garantia de cobertura integral.
- `raw/`: JSON bruto dos posts novos em chunks comprimidos.
- `_state.duckdb`: progresso por identificador/plataforma/handle e tarefas de mídia.
  Os arquivos de conteúdo continuam organizados por handle, compartilhados quando
  o mesmo perfil aparece associado a mais de um identificador.

Os caminhos são iguais no S3, sob o prefixo configurado. Escritas locais usam
arquivo temporário seguido de renomeação. JSON e Parquet são gravados separadamente;
eles não formam uma transação única.

## Armazenamento no S3

```bash
uv sync --extra s3
```

```yaml
storage:
  backend: s3
  s3:
    bucket: meu-bucket-de-pesquisa
    prefix: zeezazum/
    region: sa-east-1
    state_sync_interval_seconds: 60
```

Crie o bucket previamente. As credenciais seguem a cadeia padrão do boto3:
preferencialmente uma IAM role no EC2, ou variáveis AWS/perfil local. Restrinja as
permissões ao prefixo utilizado:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": "arn:aws:s3:::meu-bucket-de-pesquisa/zeezazum/*"
  }]
}
```

O DuckDB exige disco local: no início, o estado remoto é baixado para um arquivo
temporário. Snapshots consistentes são enviados periodicamente e no encerramento.
Uma queda abrupta pode perder atualizações posteriores ao último upload concluído;
a janela inclui o intervalo configurado e o tempo de upload. Posts já enviados são
reconciliados na execução seguinte para recuperar mídia faltante.

Execute **um único processo por destino/prefixo**, incluindo `media-sync`.
Não há coordenação entre escritores no S3. Falha de checkpoint interrompe novos
perfis e provoca uma nova tentativa ao encerrar. O estado temporário é preservado
se o upload final falhar; o caminho aparece no log.

## Operação no servidor

1. Copie ou clone o projeto em uma máquina Linux com espaço para os dados (ou use S3).
   Como ponto de partida, use 2 vCPUs e 4 GB de RAM; o consumo depende dos perfis.
2. Instale uv e rode `uv sync` (ou `uv sync --extra s3`).
3. Instale o browser e dependências: `uv run playwright install --with-deps chromium`.
4. Copie a sessão criada localmente, a tabela preparada e seu `config.yaml`.
5. Mantenha `headless: true` e valide um piloto pequeno no servidor: a troca de IP
   pode exigir nova verificação do Instagram.
6. Execute em `tmux` para acompanhar e reconectar sem encerrar a coleta.

Exemplo de transferência da sessão:

```bash
ssh ec2 'mkdir -p /home/ubuntu/zeezazum/sessions'
scp sessions/instagram_storage_state.json ec2:/home/ubuntu/zeezazum/sessions/
```

Para cron, impeça execuções sobrepostas. Exemplo **Linux**, com `flock` instalado
(e `uv` disponível no PATH do cron):

```cron
0 6 * * * cd /home/ubuntu/zeezazum && flock -n /tmp/zeezazum.lock uv run zeezazum scrape --config config.yaml >> logs/cron.log 2>&1
```

Crie `logs/` antes de cadastrar o cron. Use o mesmo lock para qualquer operação
sobre esse destino. Não aumente a concorrência de perfis para acelerar lotes:
divida o input e execute lotes sequenciais. Dimensione o disco com um piloto,
medindo bytes por conta e tempo real de coleta.

## Diagnóstico e recuperação

| Situação | Como agir |
| --- | --- |
| Sessão expirada ou verificação obrigatória | O lote para de visitar novos perfis. Refaça `scripts/local_login.py`, copie a sessão e repita o comando. Downloads já aceitos são finalizados antes de sair. |
| Download falhou ou processo foi interrompido | Repita `scrape`/`backfill`, ou rode `media-sync` com o mesmo input e destino. |
| URL de mídia expirou | `media-sync` não renova URLs. Reencontre o post numa coleta ou backfill que alcance sua posição; a URL será atualizada preservando a fotografia de métricas. |
| Falha no upload final do estado | Preserve o DuckDB temporário indicado no log. Com todas as execuções paradas, copie-o para uma saída local ou envie-o como `<prefix>/_state.duckdb` ao S3 usando suas ferramentas AWS; depois repita o comando. Não substitua um estado remoto mais recente sem conferência. |
| Perfil indisponível | É registrado como erro individual e o lote continua. Confira o log e a conta antes de tentar novamente. |
| `posts.json` ilegível | A execução falha sem sobrescrever o arquivo. Preserve-o e restaure uma cópia válida antes de repetir. |

Códigos de saída:

- `0`: execução concluída sem falhas de download; confira o log para erros individuais de perfil.
- `1`: falha operacional ou downloads pendentes; examine a mensagem e o log.
- `2`: erro de uso/validação de opções da CLI.
- `3`: sessão expirada ou verificação obrigatória detectada.

Não interprete um perfil sem posts capturados como prova de que a conta está vazia.
A detecção de autenticação usa redirecionamentos conhecidos; mudanças no Instagram
podem exigir atualização do scraper. Respostas HTTP `401/403` isoladas não são
classificadas automaticamente como sessão expirada.

## Atualização de versões anteriores

A v0.2.3 sucede diretamente a v0.2.1; não houve v0.2.2. Pare as execuções e faça
backup da saída antes de atualizar. Na primeira abertura do estado antigo:

- O banco é migrado em transação; uma cópia `.pre-v023.bak` é criada ao lado do
  arquivo DuckDB usado na migração (temporário, quando o backend é S3).
- O estado existente fica associado ao handle registrado. Outras contas da mesma
  pessoa passam a ter progresso independente.
- Posts e mídias existentes são mantidos; pendências são reconstruídas a partir
  dos posts e dos arquivos presentes para as contas do input.

Colisões entre contas em versões anteriores podem já ter perdido marcadores:
a migração não consegue recriar esse histórico. Não use versões antigas sobre o
banco migrado; para voltar, restaure o backup com todos os processos parados.

## Desenvolvimento

```bash
uv run python -m unittest discover -s tests -v
```

Os testes usam arquivos temporários e simulam rede, navegador e backend remoto;
não acessam contas reais nem buckets AWS.

O código está em `src/zeezazum`: `input/` prepara contas, `platforms/instagram/`
extrai e baixa conteúdo, `pipeline/` coordena estado e recuperação, e `storage/`
implementa os destinos local e S3. A interface de scraper está em `platforms/base.py`;
configuração e orquestração ainda precisam ser adaptadas para suportar outra rede.
