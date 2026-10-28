# MemPalace MCP

O [MemPalace](https://github.com/MemPalace/mempalace) é um sistema de memória
local para assistentes de IA. Ele armazena o conteúdo original de conversas
e permite recuperá-lo por busca semântica, organizado em alas, salas e
gavetas para separar projetos e assuntos.

Este diretório configura o servidor HTTP nativo do MemPalace com Docker
Compose. O código principal é fornecido pela imagem `mempalace:3.11.0`.

## Estrutura

| Caminho | Função | Versionar? |
| --- | --- | --- |
| `docker-compose.yaml` | Serviço MCP por HTTP | Sim |
| `.env.example` | Template da configuração local | Sim |
| `.env` | Chave HTTP e caminhos internos do container | Não |
| `LICENSE` | Licença MIT dos arquivos deste diretório | Sim |
| `data/` | Banco e dados persistentes do palácio | Não |
| `cache/` | Cache do modelo ONNX de embeddings | Não |
| `backups/` | Backups locais | Não |
| `test-*-data.*` | Dados temporários de testes | Não |

## Serviço Docker

O [`docker-compose.yaml`](docker-compose.yaml) define o serviço `mempalace-mcp`.
Antes de subir, disponibilize a imagem local no daemon Docker em uso:

```bash
docker image inspect mempalace:3.11.0
```

Este diretório não inclui um Dockerfile para construí-la. Em outro host ou
daemon rootless, a imagem precisa ser construída a partir do projeto-fonte
ou importada.

O container inicia diretamente o transporte HTTP do MemPalace:

```bash
python3 -m mempalace.mcp_server --palace /data --transport http --host 0.0.0.0 --port 5001
```

As chamadas são processadas pelo servidor nativo, que mantém o processo e
reutiliza o modelo de embeddings entre requisições.

Os dados e o cache são bind mounts, para sobreviverem à recriação do
container:

```text
./data  -> /data
./cache -> /tmp/.cache
```

## API key

Antes de iniciar o serviço, crie o arquivo de configuração local:

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Preencha `MEMPALACE_MCP_HTTP_TOKEN` no `.env` com a chave gerada. O Compose
carrega esse arquivo por `env_file`, e o servidor nativo exige a chave no
header das requisições:

```http
Authorization: Bearer <sua-chave>
```

Configure esse header em cada cliente MCP e script que acessa o serviço.
Sem a chave, ou com uma chave incorreta, o acesso a `/mcp` e `/statusz`
retorna `401 Unauthorized`. O endpoint `/healthz` continua acessível sem
autenticação para o healthcheck.

O `.env` é ignorado pelo Git; versione somente o `.env.example`, sem uma
chave real. Para trocar a chave, atualize o `.env`, recrie o serviço com
`docker compose up -d` e atualize os clientes.

## Execução sem root no container

O processo roda com UID/GID `1000:1000` por padrão. Para usar os IDs do seu
usuário, execute na pasta deste README:

```bash
export MEMPALACE_UID="$(id -u)"
export MEMPALACE_GID="$(id -g)"
mkdir -p data cache
docker compose up -d
```

Os diretórios `data/` e `cache/`, incluindo os arquivos existentes, precisam
permitir leitura e escrita para esse UID/GID. Se uma instalação anterior
criou arquivos como root, o dono ou administrador precisará corrigir essas
permissões uma vez.

O `.env` também define `HOME=/tmp` e `XDG_CACHE_HOME=/tmp/.cache` para o
container. Esses valores precisam permanecer alinhados ao destino do volume
de cache no Compose.

`HOME=/tmp` fornece um diretório gravável sem root. O Chroma resolve o cache
do modelo por `Path.home()/.cache`; por isso o bind mount fica em `/tmp/.cache`.
Os dados continuam persistidos em `/data`; outros arquivos criados no HOME
são temporários.

Isso configura o usuário do processo no container. Para também executar o
daemon Docker sem root, o host precisa ter Docker rootless configurado.

## Conexão MCP

Configure o cliente para usar HTTP com a URL:

```text
http://127.0.0.1:5001/mcp
```

O servidor recebe chamadas JSON-RPC em `POST /mcp`. A única porta publicada
é `127.0.0.1:5001`, acessível pelo loopback do host.

O bind `0.0.0.0` dentro do container permite o encaminhamento da porta pelo
Docker. Containers na mesma rede Docker também podem alcançar o serviço,
mas precisam enviar a chave para acessar as rotas protegidas.

## Operação

Na pasta deste README:

```bash
# Subir ou recriar o serviço
docker compose up -d

# Ver estado e logs
docker compose ps mempalace-mcp
docker compose logs --tail=100 mempalace-mcp

# Verificar se o servidor HTTP está respondendo: retorna ok
curl -fsS http://127.0.0.1:5001/healthz

# Consultar estado operacional
(
  set -a
  . ./.env
  set +a
  curl -fsS -H "Authorization: Bearer ${MEMPALACE_MCP_HTTP_TOKEN}" http://127.0.0.1:5001/statusz
)

# Conferir o usuário do processo
docker compose exec mempalace-mcp id

# Conferir o healthcheck do Docker
docker inspect mempalace-mcp --format '{{.State.Health.Status}}'
```

O healthcheck usa `/healthz` para verificar a disponibilidade do servidor
HTTP. A prontidão da busca deve ser conferida com uma chamada MCP, pois o
modelo de embeddings pode ser carregado na primeira consulta.

## Persistência e Git

Não faça commit do `.env`, de `data/`, `cache/`, `backups/` ou diretórios de teste. Eles
podem conter o palácio pessoal, bancos SQLite, arquivos WAL/SHM e artefatos
grandes do modelo. Esses caminhos estão cobertos pelo `.gitignore` local.

Antes de publicar, confira:

```bash
git status --short
git diff -- README.md .gitignore .env.example docker-compose.yaml
```

## Licença

Os arquivos deste diretório são distribuídos sob a [licença MIT](LICENSE).
O [projeto original MemPalace](https://github.com/MemPalace/mempalace)
possui sua própria [licença MIT](https://github.com/MemPalace/mempalace/blob/develop/LICENSE).
