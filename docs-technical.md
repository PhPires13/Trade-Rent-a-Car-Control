# Planejamento Técnico — Trade Rent a Car

**Stack e arquitetura de desenvolvimento** · complementa o PRD ([docs.md](docs.md))

*(decidido em 18/07/2026 com o desenvolvedor: Python, hospedagem em cloud barata, arquitetura full-stack integrada)*

## 1. Resumo da stack

| Camada | Escolha | Por quê |
|--------|---------|---------|
| Linguagem | **Python 3.14+** | Familiaridade do desenvolvedor. |
| Framework | **Django 5.x** (full-stack) | Um projeto só: ORM, migrações, auth, admin e templates resolvem quase toda a spec; ideal para dev solo e app interno de 2 usuários. |
| Banco | **PostgreSQL 16** (gerenciado) | Relacional (o domínio é todo relacional), confiável, free tier disponível; backups fáceis. |
| Front | **Templates Django + HTMX + Alpine.js + Tailwind CSS** | Interatividade suficiente (filtros, formulários dinâmicos, baixa de recebimento) sem SPA — sem build complexo nem segundo projeto. Tema com as cores da marca (amarelo `#F0C948` / preto `#1A1A1A`). |
| Gráficos | **Chart.js** (via CDN/estático) | Basta para o painel (ocupação, custos, ranking). |
| Exportações | **openpyxl** (Excel), **CSV nativo**, **WeasyPrint** (PDF) | Cobre a exportação para a contabilidade (Seção 5 do docs.md). |
| Jobs agendados | **Management commands + cron da plataforma** (1 execução diária) | Gera cobranças do dia, marca atrasos/inadimplência, recalcula alertas (FICI, preventivas, auxílio >7 dias, KM pendente). Sem Celery/filas — escala de 20 carros não justifica. |
| Auditoria | **django-simple-history** | "Todo lançamento guarda autor e data/hora" (Seção 2 do docs.md) sem esforço; histórico de alterações por registro. |
| Edição simultânea | **django-concurrency** (lock otimista) | O aviso "vale a última gravação, com alerta" da Seção 2. |
| Auth | **Django auth nativo** (2 usuários staff) | Sem necessidade de OAuth/allauth; senhas fortes + sessão. Credenciais dos órgãos criptografadas no banco (ex.: `django-fernet-encrypted-fields`). |

## 2. Hospedagem e operação

- **Plataforma: Railway** (plano Hobby: **US$ 5/mês fixos, já incluindo US$ 5 de créditos de uso**; excedente cobrado por consumo — CPU US$ 20/vCPU/mês, RAM US$ 10/GB/mês). Um app Django pequeno + Postgres pequeno no próprio Railway costuma fechar entre **US$ 6–12/mês**. Deploy por push no GitHub, cron incluso, zero administração de servidor.
- **Custo mínimo (recomendado): Postgres no Neon (free tier, 0,5 GB) + só o app no Railway** — o consumo do app fica dentro do crédito e o total trava nos **US$ 5/mês**. Se preferir tudo num lugar só, Postgres no próprio Railway (US$ 6–12/mês). Alternativas equivalentes: Fly.io, Render (free tier do Render hiberna — aceitável só se custo for crítico).
- **SQLite não vai para produção** — serviria tecnicamente para 2 usuários, mas exige volume persistente + backup manual (Litestream), impede inspecionar o banco remotamente e cria diferenças dev/prod nas migrações. Com o free tier do Neon, o Postgres gerenciado custa zero — não há economia que justifique. SQLite fica liberado apenas para desenvolvimento local nos primeiros dias, antes de subir o Postgres via Docker.
- **Arquivos estáticos** servidos pelo próprio Django com WhiteNoise (sem CDN). Anexos (Fase 2): storage S3-compatível (Cloudflare R2, free tier).
- **Backup**: dump diário automático do Postgres (backup nativo do Railway + `pg_dump` semanal baixado localmente — os dados da empresa não podem depender de um único provedor).
- **Domínio/HTTPS**: subdomínio da plataforma no início; domínio próprio depois, HTTPS automático.

## 3. Estrutura do projeto (apps Django ↔ módulos da spec)

```
traderentacar/
  config/            # settings, urls, cron entrypoints
  apps/
    frota/           # 4.1 veículos, categorias, proteção/seguro, fornecedores, órgãos
    pessoas/         # 4.1 clientes, condutores autorizados
    alocacoes/       # 4.2 alocações, trocas temporárias, linha do tempo
    financeiro/      # 4.3/4.4 cobranças, recebimentos, NDs, encargos, caução, DAS
    manutencao/      # 4.5 manutenções, plano de preventivas, dias parado
    sinistros/       # 4.6 sinistros, eventos, auxílio motorista
    multas/          # 4.7 multas, FICI, NIC
    km/              # 4.8 registros mensais de KM
    desmobilizacao/  # 4.9 compra/venda, ficha financeira, indicadores
    painel/          # 5. dashboard, alertas, relatórios, exportações
```

- Regras de negócio em serviços/módulos por app (não em views), testadas com **pytest-django** — as regras da Seção 4 do docs.md (classificação fiscal, encargos 5%/10%, gatilhos de preventiva, % recuperado) são código crítico e testável.
- Qualidade: **ruff** (lint/format), **pre-commit**, CI simples no **GitHub Actions** (lint + testes a cada push).
- Dependências com **uv** (`pyproject.toml`).

## 4. Decisões conscientes (o que ficou de fora e por quê)

- **Sem SPA (React/Next)** — dois usuários internos; HTMX cobre a interatividade com uma fração da complexidade.
- **Sem microserviços/API separada** — monólito Django; se um dia precisar de API (app do cliente, Fase 3), o Django REST Framework é adicionado ao mesmo projeto.
- **Sem Celery/Redis** — um cron diário resolve; cobranças e alertas não precisam de tempo real.
- **Django Admin** apenas como retaguarda (cadastros raros, correções) — as operações do dia a dia (baixa de recebimento, multas, painel) ganham interface própria, pois o Admin não acomoda bem os fluxos da Seção 4.
- **Importação de planilhas** segue opcional (decisão nº 9 do docs.md) — a estrutura de dados não depende dela.

## 5. Ordem de implementação (alinhada ao roadmap)

1. **Fundação**: projeto, auth, deploy, CI, modelos base (veículo, cliente, categoria).
2. **Prioridade nº 1 dos donos**: KM mensal + plano de preventivas com alertas (decisões nº 6, 12 e 20 do docs.md).
3. Alocações + trocas temporárias + linha do tempo.
4. Financeiro: cobranças semanais, recebimentos, encargos, NDs, caução, classificação fiscal/DAS.
5. Multas (FICI/NIC) + órgãos; sinistros/eventos + auxílio motorista.
6. Desmobilização: ficha financeira, indicadores, ranking.
7. Painel consolidado, relatórios e exportações para contabilidade.

Cada etapa entrega algo usável pelos donos — a partir da etapa 2 o sistema já substitui o controle mais crítico (preventivas).
