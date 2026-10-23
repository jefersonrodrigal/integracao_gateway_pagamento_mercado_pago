# API de PAgamento — Mercado Pago Checkout Transparente

API REST assíncrona para pedidos e pagamentos integrada ao **Mercado Pago Checkout
Transparente via Orders API** (`/v1/orders`): Pix, cartões de crédito/débito (com
autorização e captura) e boleto, com webhooks verificados, idempotência em dois níveis,
reconciliação, cancelamento e reembolso.

> **Status:** pronta para **homologação**. Ainda **não** está pronta para produção: veja
> [Checklist de produção](#checklist-de-produção) e [Pendências](#pendências-conhecidas).

---

## Sumário

1. [Arquitetura](#arquitetura)
2. [Tecnologias](#tecnologias)
3. [Instalação local](#instalação-local)
4. [Variáveis de ambiente](#variáveis-de-ambiente)
5. [Execução com Docker](#execução-com-docker)
6. [PostgreSQL e migrations](#postgresql-e-migrations)
7. [Autenticação](#autenticação)
8. [Endpoints e exemplos](#endpoints-e-exemplos)
9. [Códigos de erro](#códigos-de-erro)
10. [Idempotência e concorrência](#idempotência-e-concorrência)
11. [Fluxos de pagamento](#fluxos-de-pagamento)
12. [Estados e mapeamento do Mercado Pago](#estados-e-mapeamento-do-mercado-pago)
13. [Notificações (webhooks)](#notificações-webhooks)
14. [Testes](#testes)
15. [Homologação (sandbox)](#homologação-sandbox)
16. [Deploy](#deploy)
17. [Monitoramento](#monitoramento)
18. [Diagnóstico de problemas](#diagnóstico-de-problemas)
19. [Checklist de produção](#checklist-de-produção)
20. [Pendências conhecidas](#pendências-conhecidas)

---

## Arquitetura

```text
app/
├── main.py                 # factory FastAPI (uvicorn --factory app.main:create_app)
├── container.py            # composição: engine, cliente MP, serviços
├── worker.py               # worker durável: fila de webhooks, reconciliação, retenção
├── core/                   # config, segurança (JWT), logging, erros, middlewares, métricas, dinheiro
├── domain/states.py        # estados internos e transições permitidas
├── api/                    # deps, health/metrics e endpoints v1 (controllers finos)
├── schemas/                # modelos Pydantic de entrada/saída (nunca entidades ORM)
├── models/                 # SQLAlchemy 2.x
├── repositories/           # acesso a dados (locks explícitos: FOR UPDATE / SKIP LOCKED)
├── services/               # regras de negócio
│   ├── order_service.py        # preço no servidor, estoque, cupom
│   ├── checkout_service.py     # iniciar pagamento, captura, cancelamento
│   ├── payment_service.py      # aplica estado verificado do MP, reconciliação
│   ├── refund_service.py       # reembolso total/parcial
│   ├── webhook_service.py      # recebimento, deduplicação, processamento, dead letter
│   ├── idempotency_service.py  # Idempotency-Key interna
│   └── reconciliation_service.py
├── integrations/mercadopago/   # cliente HTTPX tipado, payloads, mapeamento, assinatura
└── db/                     # base declarativa e sessão assíncrona
alembic/                    # migrations
tests/{unit,integration,e2e}  # + tests/fakes/mercadopago.py (simulador da Orders API)
```

**Princípios**

- Controllers apenas validam, autenticam e delegam; regras ficam nos serviços.
- **Nenhuma transação PostgreSQL fica aberta durante chamadas ao Mercado Pago**: cada
  operação crítica é `TX curta → rede → TX curta`.
- Estado financeiro só muda a partir de uma **resposta da API do Mercado Pago**
  (criação, `GET /v1/orders/{id}`, cancelamento, reembolso) — nunca do corpo do webhook
  ou de dados do cliente.
- Ordem de locks fixa (pedido → pagamento → reembolso) para evitar deadlocks.
- Cada **tentativa de pagamento** interna corresponde a **uma order no Mercado Pago**
  (a Orders API aceita uma única transação de pagamento por order). Um pedido pode ter
  várias tentativas (ex.: cartão recusado e depois Pix), mas **no máximo uma ativa**.

**Por que HTTPX e não o SDK oficial?** O SDK Python oficial é síncrono; usá-lo exigiria
threads para não bloquear o event loop e daria menos controle sobre timeouts, retries e
`X-Idempotency-Key`. O cliente em `app/integrations/mercadopago/client.py` é isolado,
tipado e cobre exatamente os endpoints usados.

## Tecnologias

Python 3.15 (compatível ≥ 3.12) · FastAPI · Uvicorn · Pydantic v2 / pydantic-settings ·
SQLAlchemy 2.x async + asyncpg · Alembic · HTTPX · PyJWT · prometheus-client ·
Pytest + pytest-asyncio · Ruff · Mypy (strict) · Docker / Docker Compose · uv (lock).

## Instalação local

```bash
# 1. Dependências (uv usa o uv.lock; requer Python >= 3.12)
pip install uv
uv sync                       # cria .venv com dependências + grupo dev

# 2. Configuração
cp .env.example .env          # preencha DATABASE_URL, credenciais e JWT_SECRET

# 3. Banco e dados de exemplo
#    antes, crie o banco: veja "Criar o banco de dados" (CREATE DATABASE payments ...)
uv run alembic upgrade head
uv run python -m app.scripts.seed

# 4. API e worker (terminais separados)
uv run uvicorn app.main:app --reload --port 8000   # ou: --factory app.main:create_app
uv run python -m app.worker
```

Swagger: <http://localhost:8000/docs> · ReDoc: `/redoc` · OpenAPI: `/openapi.json`
(desativados em produção).

> No Python 3.15, use `uvicorn` sem o extra `[standard]` (o `pyyaml` ainda não publica
> wheel para 3.15). O servidor usa `h11` + `asyncio`, sem impacto funcional.

## Variáveis de ambiente

Todas são validadas na inicialização; faltas ou valores inválidos geram
`ConfigurationError` com o **nome** da variável e o problema, **sem** expor valores.
Lista completa e comentada em [`.env.example`](.env.example).

| Variável | Obrigatória | Descrição |
|---|---|---|
| `APP_ENV` | não | `development`, `test`, `staging`, `production` (padrão `development`) |
| `DATABASE_URL` | **sim** | `postgresql+asyncpg://…` (driver assíncrono obrigatório) |
| `MERCADO_PAGO_ACCESS_TOKEN` | **sim** | Access Token (somente backend) |
| `MERCADO_PAGO_WEBHOOK_SECRET` | **sim** | Assinatura secreta dos Webhooks (segredo **diferente** do Access Token) |
| `JWT_SECRET` | **sim** | Chave HMAC dos JWTs (≥ 32 caracteres) |
| `JWT_ISSUER` / `JWT_AUDIENCE` | não | Validados em todo token |
| `FORCE_HTTPS` | prod | Rejeita HTTP e envia HSTS (obrigatório em produção) |
| `TRUST_PROXY_HEADERS` | não | Usa `X-Forwarded-For/Proto` (somente atrás de proxy confiável) |
| `CORS_ALLOWED_ORIGINS` | não | Origens permitidas, separadas por vírgula (`*` proibido em produção) |
| `RATE_LIMIT_PER_MINUTE` | não | Limite por IP (API); webhooks têm limite próprio |
| `PIX_EXPIRATION` / `BOLETO_EXPIRATION` | não | Duração ISO 8601 (`PT1H`, `P3D`) |
| `SHIPPING_FLAT_FEE` / `FREE_SHIPPING_THRESHOLD` | não | Regras de frete |
| `MAX_INSTALLMENTS` / `MAX_ORDER_AMOUNT` | não | Limites comerciais |

Em `production` a aplicação recusa iniciar se `FORCE_HTTPS=false`, `DOCS_ENABLED=true`,
`CORS` contiver `*` ou a URL do Mercado Pago não for HTTPS.

## Execução com Docker

```bash
cp .env.example .env            # preencha MERCADO_PAGO_* e JWT_SECRET
docker compose up --build       # db → migrate (alembic upgrade head) → api + worker
docker compose exec api python -m app.scripts.seed
docker compose exec api python -m app.scripts.issue_token --sub cliente-123
```

Serviços: `db` (PostgreSQL 17, porta só em `127.0.0.1`), `migrate` (executa uma vez),
`api` (porta 8000, healthcheck em `/health/live`), `worker`. A imagem é multi-stage,
roda como usuário não-root e instala dependências estritamente pelo `uv.lock`.

## PostgreSQL e migrations

### Criar o banco de dados

As migrations criam as **tabelas**, mas o **banco** (e os usuários) precisam existir antes
de rodar `alembic upgrade head`. Execute o SQL conectado como superusuário, por exemplo
`psql -U postgres` (ou no Query Tool do pgAdmin, conectado ao banco `postgres`).

> No Windows, se o banco não existir, o `asyncpg` mostra o erro como
> *"connection was closed in the middle of operation"*.

**Opção A — desenvolvimento local (mínima):** usa o usuário `postgres` já existente.

```sql
CREATE DATABASE payments ENCODING 'UTF8' TEMPLATE template0;
```

```dotenv
DATABASE_URL=postgresql+asyncpg://postgres:<senha>@localhost:5432/payments
```

**Opção B — homologação/produção (recomendada):** separa o dono do esquema, que roda as
migrations, do usuário da aplicação, que só lê e grava dados e não pode alterar tabelas.

```sql
-- 1. Usuários (troque as senhas)
CREATE ROLE payments_owner LOGIN PASSWORD 'troque-senha-owner';
CREATE ROLE payments_app   LOGIN PASSWORD 'troque-senha-app';

-- 2. Banco
CREATE DATABASE payments
    OWNER payments_owner
    ENCODING 'UTF8'
    TEMPLATE template0;

-- 3. Permissões (conectado ao novo banco)
\connect payments

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO payments_app;

-- Tabelas e sequences que as migrations (payments_owner) criarem no futuro:
ALTER DEFAULT PRIVILEGES FOR ROLE payments_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO payments_app;
ALTER DEFAULT PRIVILEGES FOR ROLE payments_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO payments_app;
```

`\connect` é um comando do `psql`. No pgAdmin, abra um novo Query Tool no banco
`payments` e execute a partir do passo 3.

Use cada usuário na sua etapa:

```bash
# Migrations: dono do esquema
DATABASE_URL=postgresql+asyncpg://payments_owner:<senha>@localhost:5432/payments uv run alembic upgrade head
```

```dotenv
# .env da API e do worker: usuário da aplicação
DATABASE_URL=postgresql+asyncpg://payments_app:<senha>@localhost:5432/payments
```

No PowerShell, defina a variável antes do comando:
`$env:DATABASE_URL="postgresql+asyncpg://payments_owner:<senha>@localhost:5432/payments"; uv run alembic upgrade head`.

**Banco de testes (opcional):** os testes de integração recriam o esquema, usam
`TRUNCATE` e criam um banco temporário para verificar as migrations. Por isso precisam
de um usuário dono do banco com permissão `CREATEDB`. Use apenas em desenvolvimento:

```sql
CREATE DATABASE payments_test ENCODING 'UTF8' TEMPLATE template0;  -- com o usuário postgres
-- ou, com usuário próprio:
-- ALTER ROLE payments_owner CREATEDB;
-- CREATE DATABASE payments_test OWNER payments_owner ENCODING 'UTF8' TEMPLATE template0;
```

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:<senha>@localhost:5432/payments_test uv run pytest
```

Com Docker Compose nada disso é necessário: o serviço `db` já cria o banco `payments`.

### Migrations

```bash
uv run alembic upgrade head          # aplicar
uv run alembic downgrade -1          # reverter a última
uv run alembic revision --autogenerate -m "descricao"   # nova migration (revise o arquivo!)
```

Tabelas: `products`, `coupons`, `orders`, `order_items`, `payments`, `refunds`,
`payment_events`, `idempotency_records`, `state_transitions` (auditoria/outbox).

Restrições relevantes:

- `uq_payments_one_active_per_order` — índice único **parcial**: no máximo uma tentativa
  ativa (`created`, `unknown`, `pending`, `in_review`, `authorized`) por pedido.
- `uq_refunds_one_pending_per_payment` — um reembolso em andamento por pagamento.
- `uq_idempotency_principal_scope_key`, `uq_payment_events_dedup_key`, unicidade de
  `external_reference`, `provider_order_id`, `provider_payment_id`, chaves idempotentes.
- `CHECK`s de valores (`total = subtotal - desconto + frete`, `refunded_amount <= amount`,
  estoque ≥ 0) e de domínio dos estados.
- Sessões com `statement_timeout` e `idle_in_transaction_session_timeout`.

**Retenção** (worker, a cada hora): registros de idempotência expirados são removidos
(`IDEMPOTENCY_TTL_HOURS`, padrão 24 h); o corpo bruto de eventos processados é apagado após
`PAYMENT_EVENT_RETENTION_DAYS` (padrão 365), **mantendo** a linha do evento para auditoria.
Pedidos, pagamentos, reembolsos e `state_transitions` não são apagados (conciliação).

## Autenticação

Endpoints privados exigem `Authorization: Bearer <JWT>` HS256 com `sub`, `exp`, `iss`,
`aud` e `roles`:

| Papel | Permissões |
|---|---|
| `customer` | criar pedido; consultar, pagar e cancelar **os próprios** pedidos |
| `admin` | tudo de `customer` em qualquer pedido + captura, reembolso, reprocessar eventos |

Pedidos de outro cliente retornam **404** (não 403) para não revelar sua existência
(proteção contra IDOR). Em produção, os tokens devem vir do seu provedor de identidade;
para desenvolvimento/homologação:

```bash
python -m app.scripts.issue_token --sub cliente-123
python -m app.scripts.issue_token --sub operador-1 --role admin --hours 8
```

O webhook é público, protegido pela assinatura `x-signature`.

## Endpoints e exemplos

Base: `/api/v1`. Valores monetários são **strings** com 2 casas (`"138.87"`), moeda BRL.
Todos os `POST` (exceto o webhook) exigem `Idempotency-Key` (8–128 caracteres
`[A-Za-z0-9_-:.]`). Documentação detalhada de cada operação (regras, códigos, efeitos no
banco) está no Swagger.

| Método | Endpoint | Papel | Finalidade |
|---|---|---|---|
| POST | `/orders` | customer | Criar pedido |
| GET | `/orders/{order_id}` | dono/admin | Consultar pedido |
| POST | `/orders/{order_id}/payments` | dono/admin | Iniciar pagamento |
| GET | `/orders/{order_id}/payment-status?refresh=` | dono/admin | Estado do pagamento (opcionalmente reconciliado) |
| POST | `/orders/{order_id}/capture` | admin | Capturar cartão autorizado |
| POST | `/orders/{order_id}/cancel` | dono/admin | Cancelar pedido não pago |
| POST | `/orders/{order_id}/refunds` | admin | Reembolso total/parcial |
| GET | `/payment-methods` | autenticado | Meios disponíveis na conta |
| POST | `/webhooks/mercadopago` | público (assinado) | Notificações |
| POST | `/webhooks/events/{event_id}/reprocess` | admin | Reprocessar evento |
| GET | `/health/live`, `/health/ready`, `/metrics` | infra | Saúde e métricas |

### Criar pedido

```bash
curl -X POST http://localhost:8000/api/v1/orders \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: pedido-$(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"sku":"CAMISETA-P","quantity":2},{"sku":"CANECA-01","quantity":1}],
       "coupon_code":"BEMVINDO10","reference":"loja-42"}'
```

```json
{"id":"a3f42c51-…","status":"pending_payment","subtotal":"154.30","discount_total":"15.43",
 "shipping_total":"0.00","total_amount":"138.87","currency":"BRL","items":[…]}
```

O cliente envia **apenas SKU e quantidade**. Campos como `unit_price` ou `total_amount`
são rejeitados (422). Arredondamento: `ROUND_HALF_UP` em centavos, por linha, desconto e frete.

### Iniciar pagamento

```bash
# Pix
-d '{"method":"pix","payer":{"email":"comprador@testuser.com"}}'

# Cartão (token gerado no front-end pelo MercadoPago.js / Card Payment Brick)
-d '{"method":"credit_card","token":"<CARD_TOKEN>","payment_method_id":"master",
     "installments":3,"capture":true,
     "payer":{"email":"comprador@testuser.com","identification":{"type":"CPF","number":"19119119100"}}}'

# Boleto
-d '{"method":"boleto","payer":{"email":"comprador@testuser.com","first_name":"Ana","last_name":"Silva",
     "identification":{"type":"CPF","number":"19119119100"},
     "address":{"zip_code":"06233903","street_name":"Av. das Nações Unidas","street_number":"3003",
                "neighborhood":"Bonfim","city":"Osasco","state":"SP"}}}'
```

Resposta `201` (`PaymentOut`): `status`, `provider_order_id`, `provider_status`,
`provider_status_detail`, `outcome_unknown`, e `pix` (`qr_code`, `qr_code_base64`,
`ticket_url`) ou `boleto` (`digitable_line`, `barcode_content`, `ticket_url`).
Recusa do emissor também retorna `201` com `status: "rejected"` (é um resultado de negócio).

## Códigos de erro

Formato único: `{"error": {"code", "message", "details?", "request_id"}}`. Erros de
validação **não** ecoam os valores enviados (podem conter token, CPF, e-mail).

| HTTP | `code` | Quando |
|---|---|---|
| 401 | `unauthenticated` / `invalid_signature` | JWT ausente/inválido; webhook com assinatura inválida |
| 403 | `forbidden` / `https_required` | Papel insuficiente; HTTP em produção |
| 404 | `order_not_found` / `event_not_found` | Inexistente **ou de outro cliente** |
| 409 | `insufficient_stock` | Estoque insuficiente |
| 409 | `payment_in_progress` | Já existe tentativa ativa |
| 409 | `order_not_payable` / `order_not_cancellable` / `not_refundable` | Estado do pedido não permite |
| 409 | `payment_outcome_pending` | Resultado de cobrança em reconciliação; tente depois |
| 409 | `idempotency_request_in_progress` | Mesma chave ainda em processamento (`Retry-After`) |
| 409 | `refund_in_progress` / código do provedor | Reembolso concorrente / recusado pelo MP |
| 413 | `payload_too_large` | Corpo acima de `MAX_REQUEST_BODY_BYTES` |
| 422 | `validation_error` | Schema inválido |
| 422 | `product_unavailable`, `invalid_coupon`, `coupon_min_subtotal`, `invalid_installments`, `invalid_refund_amount`, `order_amount_exceeded` | Regras de negócio |
| 422 | `invalid_idempotency_key` / `idempotency_key_reused` | Chave ausente / reutilizada com outro corpo |
| 429 | `rate_limited` | Limite por IP (`Retry-After`) |
| 500 | `internal_error` | Erro inesperado (detalhes só nos logs) |
| 502 | `payment_provider_error` | MP recusou a requisição por erro de integração (ex.: credencial) |
| 503 | `payment_provider_unavailable` / `service_unavailable` | MP indisponível e cobrança **não** enviada |

## Idempotência e concorrência

**Interna (`Idempotency-Key`)** — tabela `idempotency_records`, única por
(usuário autenticado, operação, chave):

- Mesma chave + mesmos parâmetros → devolve a resposta original
  (`Idempotent-Replayed: true`). Parâmetros diferentes → `422 idempotency_key_reused`.
- Em andamento → `409` (após `IDEMPOTENCY_LOCK_TIMEOUT_SECONDS` pode ser retomada; os
  serviços são seguros para retomada).
- Erros 5xx/transitórios liberam a chave; erros 4xx definitivos ficam gravados.
- Expira em `IDEMPOTENCY_TTL_HOURS`.

**Externa (`X-Idempotency-Key` do Mercado Pago)** — chave determinística persistida por
operação: `create-<payment_id>`, `cancel-<payment_id>`, `capture-<payment_id>`,
`refund-<refund_id>`. Retries reutilizam **a mesma chave**; nunca se gera chave nova após
timeout.

**Resultado desconhecido** (timeout de leitura, 5xx, `423 resource_locked` ou
`409 idempotency_key_already_used` em retry): a tentativa vai para `unknown` e é
**reconciliada** buscando `GET /v1/orders?external_reference=pay_<id>` (cada tentativa tem
`external_reference` exclusivo). Enquanto `unknown`, novas tentativas e cancelamento são
bloqueados. Se nada for encontrado após `UNKNOWN_PAYMENT_GIVE_UP_SECONDS`, a tentativa é
marcada `failed` (com alerta) e o pedido volta a aceitar pagamento.

**Concorrência**: `SELECT … FOR UPDATE` no pedido/pagamento, índice único parcial de
tentativa ativa, versionamento otimista (`version_id_col`) em `orders` e `payments`,
locks ordenados de produtos na reserva de estoque e `SKIP LOCKED` na fila de eventos.

## Fluxos de pagamento

```text
Pix / Boleto
  POST /payments ──► MP: action_required/waiting_transfer|waiting_payment ──► payment=pending, order=processing
  pagador paga ──► webhook (assinado) ──► GET /v1/orders/{id} ──► processed/accredited ──► approved / paid
  expiração ──► expired ──► order volta a pending_payment (nova tentativa permitida)

Cartão (captura automática)
  POST /payments ──► processed/accredited ──► approved / paid
                 └─► failed/<motivo>     ──► rejected / pending_payment (pode tentar de novo)
                 └─► processing/in_review ──► in_review / processing ──► webhook decide

Cartão (capture=false)
  POST /payments ──► action_required/waiting_capture ──► authorized (NÃO é pago)
  POST /capture (admin) ──► processed/accredited ──► approved / paid     (expira em 5 dias)

Cancelamento: pending_payment sem tentativa → local; com tentativa pendente/autorizada →
  POST /v1/orders/{id}/cancel, confirmado por consulta; estoque e cupom devolvidos.
Reembolso: POST /v1/orders/{id}/refund (total sem corpo; parcial com transactions[{id,amount}])
  ──► partially_refunded / refunded.
```

Liberação de produto/benefício: ocorre **uma única vez**, na transição do pedido para
`paid` (sob lock), registrada em `state_transitions` — use essa tabela como outbox para o
fulfillment (log `order_paid`).

## Estados e mapeamento do Mercado Pago

| MP `status` / `status_detail` (transação) | Pagamento interno | Pedido |
|---|---|---|
| `created/created`, `processing/in_process` | `pending` | `processing` |
| `processing/in_review`, `processing/pending_review_manual`, `in_review/*` | `in_review` | `processing` |
| `action_required/waiting_payment\|waiting_transfer\|pending_challenge\|waiting_retry` | `pending` | `processing` |
| `action_required/waiting_capture` | `authorized` | `processing` |
| `processed/accredited` | `approved` | `paid` |
| `processed/partially_refunded` | `partially_refunded` | `partially_refunded` |
| `refunded/refunded` ou `*/refunded` | `refunded` | `refunded` |
| `failed/*` | `rejected` | `pending_payment` |
| `failed/expired`, `canceled/expired`, `expired/*` | `expired` | `pending_payment` |
| `canceled/*` | `cancelled` | `pending_payment` → `cancelled` (via cancelamento) |
| `charged_back/*` | `charged_back` | `charged_back` |

Estados **somente internos**: `created` (registrado, ainda não confirmado pelo MP),
`unknown` (resultado incerto em reconciliação), `failed` (cobrança não criada no MP).

Diferenças registradas:

- O MP usa `canceled`; internamente `cancelled`.
- O exemplo oficial de reembolso retorna a order com `status=processed` e
  `status_detail=refunded`; tratamos `status_detail=refunded` como reembolso total.
- `charged_back/reimbursed` tem descrições divergentes entre as tabelas de status de order
  e de transação na documentação; todo `charged_back` é tratado como terminal e exige
  revisão manual.
- Combinações **não documentadas** não são adivinhadas: o estado é preservado e uma
  divergência é registrada (`financial_divergences_total{kind="unmapped_provider_status"}`).

Regressões são impedidas pela tabela de transições (`app/domain/states.py`) e pela
comparação de `last_updated_date` do MP com o último estado aplicado.

## Notificações (webhooks)

1. Painel do Mercado Pago → **Suas integrações → Webhooks → Configurar notificações**.
2. URL: `https://<seu-domínio>/api/v1/webhooks/mercadopago`; evento **Order (Mercado Pago)**.
3. Copie a **assinatura secreta** para `MERCADO_PAGO_WEBHOOK_SECRET`.

Validação (`app/integrations/mercadopago/webhooks.py`), conforme a documentação:
`x-signature: ts=<ts>,v1=<hmac>`; manifesto `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`
com `data.id` (query string) em minúsculas quando alfanumérico; HMAC-SHA256 hex em tempo
constante; tolerância de `WEBHOOK_SIGNATURE_TOLERANCE_SECONDS` sobre `ts`.

Processamento: assinatura inválida → `401` (não persistido). Válida → evento gravado em
`payment_events` (deduplicado pelo hash do conteúdo, payload **minimizado**) e resposta
`200` imediata (o MP aguarda até 22 s e reenvia a cada 15 min sem resposta). O processamento
(em background e pelo worker) consulta `GET /v1/orders/{id}`, aplica o estado com as regras
de transição e marca `processed`/`ignored`. Falhas: backoff exponencial com jitter
(30 s … 6 h) até `WEBHOOK_MAX_ATTEMPTS`, depois `dead` (log `webhook_dead_letter`,
`alert=true`). Reprocessar: `POST /api/v1/webhooks/events/{id}/reprocess` (admin).

## Testes

```bash
# PostgreSQL de teste (qualquer instância; o banco é migrado e truncado pelos testes)
export TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/payments_test

uv run pytest                    # tudo
uv run pytest tests/unit         # sem banco
uv run pytest -m e2e             # fluxos ponta a ponta
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- **Unitários**: dinheiro/arredondamento, preços e cupons, transições, mapeamento MP,
  assinatura de webhook, CPF/CNPJ, schemas, JWT, redação de logs, rate limiter, contrato e
  retries do cliente HTTP.
- **Integração**: endpoints, persistência, migrations (upgrade/downgrade e comparação
  modelos × migrations), idempotência, IDOR, estoque, limites HTTP, health/metrics.
- **E2E**: Pix pendente → confirmado; cartão aprovado/recusado/em análise; autorização +
  captura; boleto; webhook inválido/adulterado/antigo; duplicado; fora de ordem; dead letter
  e reprocessamento; timeout na criação; resultado desconhecido; tentativas concorrentes;
  pedidos concorrentes (sem overselling); cancelamento (incl. corrida com aprovação);
  reembolsos total/parcial/concorrentes/com timeout; falha de persistência após cobrança.

Os testes usam `tests/fakes/mercadopago.py`, um simulador da Orders API acoplado ao
**cliente HTTP real** via `httpx.MockTransport`. **Nenhum teste chama o Mercado Pago.**

## Homologação (sandbox)

> A Orders API **não aceita credenciais de teste** (`401 invalid_credentials`). Para
> sandbox, crie **usuários de teste** (vendedor e comprador) no painel e use as
> **credenciais de produção da conta de teste vendedora**.

1. Crie a aplicação (Checkout Transparente / Orders) e os usuários de teste.
2. Configure `.env` com o Access Token da conta de teste e o segredo do webhook.
3. Exponha a API por HTTPS (ex.: túnel) e configure o webhook (tópico Order).
4. Front-end: gere tokens de cartão com o MercadoPago.js/Card Payment Brick usando a
   Public Key da mesma conta, com os **cartões de teste** e nomes de titular oficiais
   (ex.: `APRO` aprovado, `OTHE` recusado — consulte a página de cartões de teste).
5. Roteiro mínimo:
   - [ ] Pix: criar, pagar no sandbox, receber webhook, pedido `paid`.
   - [ ] Pix: deixar expirar (`PIX_EXPIRATION=PT30M`) → `expired` e nova tentativa.
   - [ ] Cartão aprovado, recusado e em análise; parcelado.
   - [ ] Cartão com `capture=false` → captura e → cancelamento.
   - [ ] Boleto: emissão, linha digitável, confirmação.
   - [ ] Cancelamento de Pix/boleto pendente.
   - [ ] Reembolso parcial e total.
   - [ ] Webhook com assinatura inválida (401) e reenvio do mesmo evento (duplicado).
   - [ ] Validar empiricamente: formato de `ts` (s × ms), presença de `data.id` na query,
         presença de `transactions.refunds` no `GET /v1/orders/{id}` e códigos HTTP de sucesso.

## Deploy

1. Build: `docker build -t payment-api:<versão> .`
2. Banco gerenciado PostgreSQL com TLS, backups e PITR; usuário de aplicação sem DDL e
   usuário separado para migrations.
3. Execute `alembic upgrade head` como etapa de release (job único) **antes** da nova versão.
4. Rode `api` (N réplicas) e `worker` (≥ 1; múltiplos são seguros) com:
   `APP_ENV=production`, `FORCE_HTTPS=true`, `DOCS_ENABLED=false`, `TRUST_PROXY_HEADERS=true`
   e `FORWARDED_ALLOW_IPS=<ip do proxy>` atrás de load balancer TLS.
5. Segredos via gerenciador (AWS Secrets Manager, Vault, etc.), nunca na imagem.
6. Probes: liveness `/health/live`, readiness `/health/ready`. Restrinja `/metrics` à rede interna.
7. Rate limiting em memória é por réplica: aplique também no gateway/ingress.

## Monitoramento

Logs JSON em stdout com `request_id` (propague `X-Request-ID`), sem tokens, segredos,
e-mail, documentos, endereço ou dados de QR/boleto (redação automática).

Métricas Prometheus (`/metrics`):

| Métrica | Alerta sugerido |
|---|---|
| `http_requests_total`, `http_request_duration_seconds` | taxa de 5xx, p95 |
| `mercadopago_requests_total{operation,outcome}` / `_duration_seconds` | timeouts, `http_5xx`, `http_429` |
| `payment_outcome_unknown_total` | qualquer aumento sustentado |
| `webhook_events_total{result}` | `invalid_signature` em alta (ataque ou segredo errado) |
| `webhook_dead_letters_total` | **> 0** |
| `financial_divergences_total{kind}` | **> 0** (valor divergente, pago após cancelamento, status não mapeado) |
| `stale_updates_ignored_total` | informativo (eventos fora de ordem) |

`/health/ready` também expõe `webhook_backlog`, `webhook_dead_letters` e
`payments_outcome_unknown`. Logs com `"alert": true` devem gerar notificação.

## Diagnóstico de problemas

| Sintoma | Verificar |
|---|---|
| Inicialização falha com `ConfigurationError` | Mensagem lista as variáveis faltantes/inválidas |
| `alembic upgrade` falha com "connection was closed in the middle of operation" | No Windows, o asyncpg mostra assim um banco inexistente: crie-o (`CREATE DATABASE payments;`) |
| `401 invalid_credentials` do MP | Orders API exige credenciais de produção (de conta de teste no sandbox) |
| Todos os webhooks `401` | `MERCADO_PAGO_WEBHOOK_SECRET` errado; relógio do servidor (tolerância de `ts`); proxy alterando query string |
| Pedido preso em `processing` | `GET …/payment-status?refresh=true`; `payment_events` com `failed/dead`; worker rodando? |
| Pagamento `unknown` | Worker reconcilia a cada ~30 s; ver logs `payment_outcome_unknown` / `payment_given_up` |
| `409 payment_in_progress` | Há tentativa ativa; consulte o status antes de tentar outra |
| Evento `dead` | `last_error` em `payment_events`; corrija a causa e reprocesse (admin) |
| `financial_divergence` | Conciliação manual com o painel do MP usando `provider_order_id` / `external_reference` |
| Rastrear uma requisição | Filtre logs por `request_id`; respostas do MP registram `provider_request_id` |

## Checklist de produção

- [ ] Homologação completa do roteiro acima com credenciais reais de conta de teste.
- [ ] Testes automatizados verdes no CI com PostgreSQL da mesma versão de produção.
- [ ] Imagem Docker construída e escaneada (vulnerabilidades) no CI.
- [ ] Credenciais de produção apenas no gerenciador de segredos; rotação definida
      (Access Token, segredo de webhook, `JWT_SECRET`).
- [ ] TLS ponta a ponta, `FORCE_HTTPS=true`, docs desabilitadas, CORS restrito.
- [ ] Provedor de identidade real emitindo os JWTs (ou troca para RS256/JWKS).
- [ ] Rate limit no gateway; WAF; `/metrics` interno.
- [ ] Alertas configurados (dead letters, divergências, unknown, 5xx, timeouts do MP).
- [ ] Backups/PITR testados; política de retenção revisada com jurídico (LGPD).
- [ ] Revisão de escopo PCI DSS (SAQ A com tokenização no front-end: a API não recebe PAN/CVV).
- [ ] Runbook de conciliação diária com relatórios do Mercado Pago.
- [ ] Fulfillment consumindo `state_transitions` (`to_status='paid'`) de forma idempotente.
