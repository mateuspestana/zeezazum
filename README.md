# Zeezazum

Raspador de posts do Instagram (imagens e vídeos), inspirado no
[Zeeschuimer](https://github.com/digitalmethodsinitiative/zeeschuimer): em vez de
fazer scraping de DOM, captura o tráfego de rede que a própria página carrega
enquanto navega pelo perfil, via Playwright.

Pensado para ser universalizável: qualquer tabela (parquet/csv/xlsx) com uma
coluna de texto livre contendo URLs/handles de redes sociais e uma coluna de id
serve de input — não é acoplado ao domínio de candidaturas eleitorais.

**Autor:** Matheus C. Pestana ([matheus.pestana@iesp.uerj.br](mailto:matheus.pestana@iesp.uerj.br))

## Setup

```bash
uv venv
uv pip install -e .           # ou -e ".[s3]" para habilitar storage no S3
uv run playwright install chromium firefox
```

## Fluxo de uso

### 1. Preparar os dados (roda em qualquer lugar, não precisa de login)

Extrai só as contas de Instagram da coluna de texto livre (que costuma ter
várias redes separadas por `;`, prefixos como "Link Instagram:", etc.) e gera
uma tabela pequena e limpa:

```bash
uv run zeezazum prepare \
  --input data/candidatos_atual.parquet \
  --column "REDES SOCIAIS" \
  --id-column SQ_CANDIDATO \
  --platform instagram \
  --extra-columns "NM_URNA_CANDIDATO,SG_UF,SG_PARTIDO,DS_CARGO" \
  --output data/candidatos_instagram.parquet
```

`--extra-columns` é opcional e serve pra levar junto colunas de identificação da
tabela de origem (nome, UF, partido, cargo etc.), úteis pra conferir/filtrar a
lista antes de raspar. O output sempre tem `row_id`, `handle`, `instagram_url`
mais as colunas extras pedidas, uma linha por conta de Instagram encontrada
(um mesmo candidato pode aparecer mais de uma vez se tiver citado mais de uma
conta de Instagram no texto de origem).

### 2. Login (roda LOCALMENTE, no seu computador — não no servidor)

O EC2 não tem interface gráfica, então o login acontece localmente uma vez (ou
sempre que a sessão expirar):

```bash
uv run python scripts/local_login.py
```

Isso abre um browser visível, você loga manualmente no Instagram, aperta Enter
no terminal, e a sessão é salva em `sessions/instagram_storage_state.json`.
Copie esse arquivo para o servidor:

```bash
scp sessions/instagram_storage_state.json ec2:/caminho/do/projeto/sessions/
```

### 3. Configurar

Copie `config.example.yaml` para `config.yaml` e ajuste `input.path` (a saída
do passo 1), `scraping.max_posts`/`since_date`, e `storage.backend` (`local`
ou `s3`).

#### Salvando no S3 em vez de local

Por padrão (`storage.backend: local`) tudo é gravado em disco, em
`storage.local.base_path` (`output/` por padrão). Pra gravar direto no S3 —
recomendado se o disco do servidor é pequeno, ou se você quer os dados
acessíveis de outro lugar sem depender do EC2 continuar de pé — configure:

```yaml
storage:
  backend: s3
  s3:
    bucket: meu-bucket-de-pesquisa
    prefix: zeezazum/            # "pasta" dentro do bucket; pode deixar "" pra gravar na raiz
    region: sa-east-1            # região do bucket
```

A estrutura gravada no bucket é a mesma que seria gravada localmente (mesma
árvore `instagram/<handle>/posts.json`, `posts.parquet`, `media/...`, e o
`_state.duckdb`), só que sob `s3://meu-bucket-de-pesquisa/zeezazum/...` em vez
de `output/...`.

**Passo a passo pra deixar isso funcionando:**

1. **Instale o extra `s3`** (o `boto3` não vem por padrão, pra não obrigar
   quem só usa `local` a instalar o SDK inteiro da AWS):
   ```bash
   uv pip install -e ".[s3]"
   ```

2. **Crie o bucket** (se ainda não existir), na região que você vai usar em
   `storage.s3.region`:
   ```bash
   aws s3 mb s3://meu-bucket-de-pesquisa --region sa-east-1
   ```

3. **Dê credenciais AWS pro processo** — o Zeezazum usa a cadeia de
   credenciais padrão do `boto3`, então qualquer uma dessas formas funciona
   (da mais recomendada pra menos, quando rodando no EC2):
   - **IAM role anexada à instância EC2** (melhor opção no servidor — não
     precisa gerenciar chave nenhuma). Crie uma role com a policy abaixo e
     anexe à instância em EC2 → Actions → Security → Modify IAM role.
   - **Variáveis de ambiente**, se não for usar IAM role:
     ```bash
     export AWS_ACCESS_KEY_ID=...
     export AWS_SECRET_ACCESS_KEY=...
     export AWS_DEFAULT_REGION=sa-east-1
     ```
   - **`aws configure`** (grava em `~/.aws/credentials`), útil pra testar
     localmente antes de ir pro EC2.

4. **Permissões mínimas necessárias** (o Zeezazum só grava e lê objetos, não
   lista o bucket) — policy IAM de exemplo, restrita ao prefixo configurado:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::meu-bucket-de-pesquisa/zeezazum/*"
       }
     ]
   }
   ```

5. **Teste antes de rodar em escala**: rode `zeezazum scrape` com um
   `input.path` de 1-2 contas e confira no console da AWS (ou
   `aws s3 ls s3://meu-bucket-de-pesquisa/zeezazum/instagram/ --recursive`)
   que os arquivos apareceram.

**Observação:** com `storage.backend: s3`, o `output/_state.duckdb` também vai
pro bucket — ou seja, o estado de resume/raspagem incremental viaja com os
dados, não fica preso ao disco de uma instância EC2 específica (útil se você
trocar de servidor no meio do projeto).

### 4. Rodar a raspagem (no servidor, headless)

```bash
uv run zeezazum scrape --config config.yaml
```

Cada candidato é raspado sequencialmente (com um delay aleatório entre perfis,
para reduzir risco de bloqueio da conta logada). Os posts de cada perfil são
salvos assim que raspados (`posts.json` + `posts.parquet`), e o download de
imagens/vídeos daquele perfil roda em paralelo enquanto o próximo perfil já
está sendo raspado.

**A execução é segura para rodar repetidamente** (ex.: via `crontab` diário):
- Perfis novos são raspados do zero, respeitando `max_posts`/`since_date`.
- Perfis já raspados antes são atualizados incrementalmente — a raspagem para
  assim que encontra o post mais recente já conhecido, então só os posts novos
  desde a última execução são baixados.
- Se o processo for interrompido no meio, a próxima execução retoma de onde
  parou (o estado por perfil fica em `output/_state.duckdb`).

Exemplo de cron diário no EC2:

```
0 6 * * * cd /caminho/do/projeto && uv run zeezazum scrape --config config.yaml >> logs/cron.log 2>&1
```

### 5. Aprofundar histórico de contas já raspadas (opcional)

`zeezazum scrape` é incremental "pra frente no tempo": ele para assim que
reencontra o post mais recente já conhecido, então é seguro rodar todo dia mas
nunca busca posts *mais antigos* que os que você já tem. Se você quiser mais
histórico de contas que já foram raspadas (por exemplo, foi rodado com
`max_posts: 15` no começo e agora você quer mais posts antigos de cada uma),
use `backfill`:

```bash
uv run zeezazum backfill --config config.yaml --extra-posts 20
```

Isso ignora o "parar no post mais recente conhecido" e busca, para cada conta
já raspada antes, até `total_já_salvo + 20` posts no total — os que já existem
são deduplicados automaticamente, só os realmente novos (mais antigos) são
baixados. Contas que nunca foram raspadas usam o `max_posts` normal do
`config.yaml`. Não é destinado a rodar via cron — é uma operação pontual,
disparada manualmente quando você decide aprofundar o histórico.

## Estrutura de output

```
output/
├── _state.duckdb              # estado por perfil (resume + raspagem incremental)
└── instagram/<handle>/
    ├── posts.json
    ├── posts.parquet
    └── media/<shortcode>_<n>.<ext>
```

## Deploy no EC2 (passo a passo)

Esse guia parte do princípio que os passos 1 (preparar dados) e 2 (login) do
fluxo acima já foram feitos **na sua máquina local**, e que você tem uma
instância EC2 (Linux, sem interface gráfica) já criada e acessível via SSH.

### 1. Provisionar a instância

Qualquer Linux recente serve (testado com Ubuntu/Amazon Linux). Recomendado:
- Pelo menos 2 vCPUs / 4GB RAM (Playwright + Chromium consomem memória).
- Disco com espaço de sobra: cada conta raspada pode facilmente passar de
  50-150MB em mídia (fotos + vídeos), então planeje o disco pelo tamanho do
  seu lote (ex.: 18 mil contas × ~50MB médios = ~900GB só de mídia — veja a
  seção "Escala e tempo estimado" abaixo antes de decidir o tamanho do disco).
- Se for usar `storage.backend: s3`, a instância precisa de uma IAM role (ou
  credenciais AWS) com permissão de escrita no bucket — não é preciso disco
  grande nesse caso, já que a mídia vai direto pro S3.

Instale dependências do sistema que o Chromium do Playwright precisa (em
Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y python3 python3-pip curl
curl -LsSf https://astral.sh/uv/install.sh | sh   # instala o uv
source $HOME/.local/bin/env                         # ou reabra o terminal
```

### 2. Levar o projeto pro servidor

```bash
# na sua máquina local
rsync -avz --exclude .venv --exclude output --exclude sessions \
  /Users/mateuspestana/Documents/Datasets/Zeezazum/ ec2:/home/ubuntu/zeezazum/
```

(ou `git clone` se o projeto estiver num repositório remoto)

### 3. Setup do ambiente no servidor

```bash
# já dentro do EC2, via SSH
cd /home/ubuntu/zeezazum
uv venv
uv pip install -e .
uv run playwright install --with-deps chromium
```

`--with-deps` instala também as bibliotecas do sistema operacional que o
Chromium precisa pra rodar headless (evita erros de biblioteca faltando, comuns
em instâncias EC2 "limpas").

### 4. Copiar o cookie de sessão

O login **não** acontece no servidor. Depois de rodar `scripts/local_login.py`
localmente (passo 2 do fluxo principal), copie o arquivo gerado:

```bash
# ainda na sua máquina local
scp sessions/instagram_storage_state.json ec2:/home/ubuntu/zeezazum/sessions/
```

### 5. Copiar os dados preparados e configurar

```bash
# na sua máquina local
scp data/candidatos_instagram.parquet ec2:/home/ubuntu/zeezazum/data/
```

No servidor, copie `config.example.yaml` para `config.yaml` e ajuste:
- `input.path`: aponte para o parquet que você acabou de copiar.
- `scraping.headless: true` (sempre, no servidor — não tem tela).
- `scraping.max_posts` / `since_date`: quantos posts por conta faz sentido pro
  seu projeto (veja a seção de escala abaixo).
- `storage.backend`: `local` (grava em `output/` no disco do EC2) ou `s3` (se
  configurado, grava direto no bucket — mais seguro contra perda de dados se a
  instância cair, e evita lotar o disco).

### 6. Rodar um piloto pequeno antes de ir pra escala

**Importante:** teste com poucas contas antes de soltar o lote inteiro. O
Instagram às vezes reage com verificação extra quando a mesma sessão logada
passa a acessar de um IP diferente (o IP do EC2, diferente do seu Mac). Rode:

```bash
uv run zeezazum scrape --config config.yaml   # com um input.path apontando pra um arquivo de teste com 5-10 contas
```

Confira `logs/zeezazum.log` em busca de erros de `ProfileUnavailable` (sinal de
sessão inválida/expirada — se acontecer logo de cara no servidor, refaça o
login local e copie o `storage_state.json` de novo) antes de apontar
`input.path` pro dataset completo.

### 7. Rodar em escala e automatizar

Depois que o piloto passar, aponte `input.path` pro dataset completo e rode:

```bash
# em background, sobrevivendo ao fechamento do SSH
nohup uv run zeezazum scrape --config config.yaml >> logs/scrape_full.log 2>&1 &
disown
```

Ou, melhor ainda, dentro de um `tmux`/`screen` pra poder reconectar e
acompanhar o progresso ao vivo:

```bash
tmux new -s zeezazum
uv run zeezazum scrape --config config.yaml
# Ctrl+B, D para "soltar" a sessão e deixar rodando; `tmux attach -t zeezazum` pra voltar
```

Pra manter o dado atualizado com posts novos, adicione ao `crontab -e` do
servidor (exemplo: todo dia às 6h):

```
0 6 * * * cd /home/ubuntu/zeezazum && uv run zeezazum scrape --config config.yaml >> logs/cron.log 2>&1
```

### Escala e tempo estimado

Com `delay_between_profiles_seconds: [4, 10]` (padrão), raspar **N** contas
sequencialmente leva aproximadamente `N × (7s de delay médio + tempo de
navegação/scroll por perfil)` — na prática, alguns segundos a poucos minutos
por conta dependendo de `max_posts`. Pra um lote de ~18 mil contas isso pode
facilmente passar de **20-40 horas** rodando sem parar, só na primeira
passada. Algumas formas de lidar com isso:

- Rodar em background por dias (`tmux`/`nohup` acima) — funciona, mas é lento
  pra ver resultado.
- Reduzir `scraping.max_posts` na primeira passada (ex.: 10-15) pra cobrir
  todas as contas mais rápido, e depois usar `zeezazum backfill` pra aprofundar
  aos poucos nas contas que interessam mais.
- Dividir o dataset em lotes (por UF, por partido, por cargo) e rodar
  `zeezazum prepare` várias vezes com `--input` filtrado, processando lote por
  lote — mais fácil de acompanhar progresso e pausar/retomar por lote.
- **Não** aumentar o paralelismo (rodar vários perfis ao mesmo tempo) sem
  repensar a arquitetura: o design atual é sequencial de propósito, porque
  todas as contas passam pela mesma sessão logada, e paralelismo agressivo
  aumenta bastante o risco de bloqueio/checkpoint na conta do Instagram usada
  pra logar.

## Estrutura do projeto

```
src/zeezazum/
├── cli.py                 # comandos: prepare, scrape, backfill
├── config.py               # validação do config.yaml (pydantic)
├── models.py                # dataclasses compartilhadas
├── input/                   # leitura de tabelas + parsing de texto livre -> contas
├── platforms/
│   ├── base.py              # interface para adicionar outras redes no futuro
│   └── instagram/            # scraper (Playwright), parser (JSON->Post), downloader (mídia)
├── storage/                  # local | s3, mesma interface
└── pipeline/
    ├── orchestrator.py       # loop sequencial + fila de mídia concorrente
    └── state.py               # estado por perfil (DuckDB)
```

## Adicionando outra plataforma

Implemente `BaseScraper` (`src/zeezazum/platforms/base.py`) para a nova rede e
plugue no `orchestrator.py` — o resto do pipeline (config, storage, estado,
fila de mídia) já é genérico.
