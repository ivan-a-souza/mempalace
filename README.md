# MemPalace — infraestrutura local

Este diretório contém os artefatos locais usados pelo serviço MCP do
MemPalace dentro do projeto [`infra`](../).

O código principal do MemPalace é fornecido pela imagem Docker
`mempalace:3.11.0`. Este diretório não é o repositório-fonte completo do
MemPalace: ele guarda os dados persistentes, o cache local do modelo de
embeddings e o bridge que adapta os transportes MCP.

## Estrutura

| Caminho | Função | Versionar? |
| --- | --- | --- |
| `mcp-tcp-bridge.py` | Bridge TCP/HTTP do MCP | Sim |
| `data/` | Banco e dados persistentes do palácio | Não |
| `cache/` | Cache do modelo ONNX de embeddings | Não |
| `backups/` | Backups locais | Não |
| `test-*-data.*` | Dados temporários de testes | Não |

## Serviço Docker

Este repositório possui um compose independente em
[`docker-compose.yaml`](docker-compose.yaml). O compose geral da infraestrutura
também mantém uma definição equivalente em
[`../docker-compose.yaml`](../docker-compose.yaml).

O serviço usa o nome `mempalace-mcp`:

```yaml
image: mempalace:3.11.0
entrypoint: python3 /mcp-tcp-bridge.py
```

Os dados e o cache são bind mounts, para sobreviverem à recriação do
container:

```text
./mempalace/data  -> /data
./mempalace/cache -> /root/.cache
```

As portas são publicadas somente no loopback do host:

| Porta | Transporte | Uso |
| --- | --- | --- |
| `127.0.0.1:5000` | TCP MCP | Clientes stdio/TCP, como Claude Code ou opencode via `socat` |
| `127.0.0.1:5001` | HTTP MCP | `POST /mcp`, SSE e healthcheck |

O serviço não fica exposto diretamente à LAN ou à internet por essas portas.

### Quem usa cada porta

- **TCP 5000:** é o transporte MCP configurado no OpenClaw, por meio de
  `socat stdio TCP:localhost:5000`. Também é adequado para clientes como
  Claude Code e opencode que esperam uma conexão stdio/TCP.
- **HTTP 5001:** é usado pelos scripts locais que chamam o MCP por JSON-RPC,
  como `workspace/scripts/mempalace-http.py`, além do healthcheck do Docker
  em `GET /health`. Também funciona como fallback quando o transporte MCP do
  gateway não está disponível.

Assim, são duas entradas para o mesmo serviço e o mesmo `/data`, não dois
servidores de memória independentes.

## Bridge

O `mcp-tcp-bridge.py` oferece duas entradas para o mesmo servidor MCP:

1. **TCP 5000:** aceita uma sessão MCP newline-delimited por conexão e
   executa um processo MCP dedicado para aquela sessão.
2. **HTTP 5001:** mantém um pool de workers MCP persistentes, reutiliza o
   handshake `initialize`, aplica timeout por chamada e reinicia workers que
   travarem ou encerrarem.

O bridge é somente transporte/adaptação. Ele não substitui o armazenamento
do MemPalace nem cria uma segunda base de memória.

## Operação

No diretório deste repositório:

```bash
# Subir ou recriar o serviço independente
docker compose up -d

# Ver estado e logs
docker compose ps mempalace-mcp
docker compose logs --tail=100 mempalace-mcp

# Healthcheck HTTP
curl -sS http://127.0.0.1:5001/health
```

> **Atenção:** não suba este compose enquanto o serviço `mempalace-mcp` do
> compose geral estiver ativo. Ambos usam o mesmo `container_name` e as portas
> 5000/5001. Para trocar de definição, pare primeiro a outra:
>
> ```bash
> docker compose -f ../docker-compose.yaml stop mempalace-mcp
> docker compose up -d
> ```

Uma resposta saudável informa `status: "ok"` e a quantidade de workers vivos.
O pool padrão tem dois workers e pode ser ajustado por
`MCP_HTTP_POOL_SIZE` no compose.

## Persistência e Git

Não faça commit de `data/`, `cache/`, `backups/` ou diretórios de teste. Eles
podem conter o palácio pessoal, bancos SQLite, arquivos WAL/SHM e artefatos
grandes do modelo. O `.gitignore` deste diretório complementa o
`.gitignore` da raiz de `infra`.

Antes de publicar o repositório, confira:

```bash
git status --short
git diff -- mempalace/mcp-tcp-bridge.py mempalace/README.md mempalace/.gitignore
```
