# Zeezazum

[![DOI](https://zenodo.org/badge/1385550085.svg)](https://doi.org/10.5281/zenodo.23932959)
[![versão](https://img.shields.io/github/v/release/mateuspestana/zeezazum?label=versão)](https://github.com/mateuspestana/zeezazum/releases)
[![licença](https://img.shields.io/github/license/mateuspestana/zeezazum?label=licença)](LICENSE)
[![atualizado](https://img.shields.io/github/last-commit/mateuspestana/zeezazum?label=atualizado)](https://github.com/mateuspestana/zeezazum/commits/main)

Coleta posts, imagens e vídeos do Instagram a partir de tabelas de contas.
Usa Playwright para capturar respostas de rede enquanto navega pelo perfil,
inspirado no [Zeeschuimer](https://github.com/digitalmethodsinitiative/zeeschuimer).
Não depende de um dataset específico: qualquer tabela com identificador e texto
contendo URLs ou handles pode alimentar a coleta.

**Versão: 0.2.5** · [Histórico de mudanças](CHANGELOG.md)

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
| `scraping.session_file` | `sessions/instagram_storage_state.json` | Sessão criada localmente |
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
| `logging.level` / `file` | `INFO` / `logs/zeezazum.log` | Registro detalhado da execução |

Perfis são visitados sequencialmente usando uma sessão. Apenas downloads de mídia
rodam em paralelo, com fila limitada. Cada download tem até três tentativas com
espera exponencial e timeout HTTP de 60 segundos.

## Arquivos produzidos

```text
output/
├── _state.duckdb
└── instagram/<handle>/
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
