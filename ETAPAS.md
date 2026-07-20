# Etapas do Desenvolvimento

Acompanhamento do plano de implementação definido no [docs-tecnico.md](docs-tecnico.md) (Seção 5). Atualizado a cada etapa concluída.

## ✅ Concluídas

### Etapa 1 — Fundação (19/07/2026)
- [x] Projeto Django 5.2 LTS + Python 3.13 (gerenciados pelo `uv`)
- [x] Configuração por variáveis de ambiente (`django-environ`), SQLite em dev / `DATABASE_URL` em produção
- [x] Autenticação: login obrigatório em todas as páginas (`LoginRequiredMiddleware`), tela de login com identidade visual
- [x] Layout base (Tailwind + HTMX + Alpine) com as cores da marca
- [x] Modelos base com histórico de alterações (`django-simple-history`): **Categoria**, **Veículo** (frota) e **Cliente**, **Condutor autorizado** (pessoas)
- [x] Django Admin como retaguarda de cadastros
- [x] Painel inicial placeholder (frota por status, clientes ativos)
- [x] Qualidade: ruff + pre-commit + pytest (8 testes) + CI no GitHub Actions
- [x] Preparação de deploy: Procfile (migrate + collectstatic + gunicorn), WhiteNoise, `.env.example`

**Pendente do usuário:** criar conta no Railway + Postgres (Neon) e fazer o primeiro deploy quando quiser publicar.

### Etapa 2 — KM mensal + Plano de preventivas (19/07/2026) — prioridade nº 1 dos donos
- [x] **Registro mensal de KM** (`/km/`): um registro por veículo/mês, KM ANT e dias herdados automaticamente do mês anterior (1º registro usa o KM da compra), médias/dia e /mês calculadas, bloqueio de KM menor que o anterior, atualização do KM atual do veículo
- [x] **Pendências do mês**: lista de veículos de locação sem leitura, com registro inline (data + odômetro) e navegação por mês
- [x] **Plano de preventivas** (`/manutencao/preventivas/`): itens confirmados pelos donos já cadastrados via migração (óleo/filtro 10 mil; alinhamento 10 mil; correia + óleo de caixa 60 mil; pneus 30 mil), intervalos personalizáveis por veículo (ex.: pneus 20 mil), lista de itens aberta
- [x] **Ciclo por item**: última execução → próxima aos X km → status Em dia / Próxima (≤1.000 km) / Vencida / Sem registro; registrar manutenção zera o ciclo
- [x] **Registro de manutenção** (formulário) e histórico por veículo — campos financeiros ficam para a etapa 5
- [x] **Painel** com alertas: preventivas vencidas/próximas por carro e leituras de KM pendentes
- [x] 25 testes passando; telas verificadas no navegador

**Nota:** o vínculo do KM/manutenção com o cliente da alocação entra na etapa 3 (alocações).

## 🔜 Próximas

### Etapa 3 — Alocações
- Vincular KM mensal e manutenções ao cliente da alocação vigente
- Alocação (valor semanal, dia de vencimento, caução opcional, KM de entrega/devolução)
- Trocas temporárias (carro substituto com ajuste de valor)
- Linha do tempo por veículo

### Etapa 4 — Financeiro
- Cobranças semanais automáticas + recebimentos com travas de lançamento
- Encargos por atraso (5%/10%, ajustável) e inadimplência (1 dia)
- Notas de débito (numeração automática) e caução
- Classificação fiscal (base do DAS) e cobrança judicial

### Etapa 5 — Multas e Sinistros
- Multas com FICI/NIC, órgãos autuadores (credenciais protegidas)
- Sinistros/eventos Auto Truck + auxílio motorista profissional (>7 dias)
- Manutenções completas (custo real × cobrado, dias parado)

### Etapa 6 — Desmobilização
- Ficha financeira por veículo (% do investimento recuperado)
- Indicadores e recomendação de venda, ranking da frota

### Etapa 7 — Painel e relatórios
- Painel consolidado com todos os alertas
- Relatórios + exportação para contabilidade (Excel/CSV/PDF)
