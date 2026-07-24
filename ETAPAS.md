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

### Etapa 3 — Alocações (19/07/2026)
- [x] **Alocação** (`/alocacoes/`): veículo disponível + cliente ativo, valor semanal, dia de vencimento (padrão: dia da semana da entrega), caução acordada (opcional), KM de entrega, limite de km (ilimitado/limitado com franquia e taxa)
- [x] **Regras**: uma alocação ativa por veículo (constraint no banco), status do veículo automático (Alocado ↔ Disponível), aviso de CNH vencida ao alocar, bloqueio de KM de devolução menor que o de entrega
- [x] **Encerramento** com KM de devolução (acerto de caução fica para a etapa 4)
- [x] **Trocas temporárias**: substituto disponível, valor semanal ajustado opcional (categoria diferente), uma troca ativa por alocação, devolução obrigatória antes de encerrar; substituto muda de status automaticamente
- [x] **Cliente vigente por data** (`cliente_vigente`): resolve quem estava com o carro considerando trocas — base para multas/sinistros (etapa 5); coluna Cliente na tela de KM mensal
- [x] **Linha do tempo por veículo**: alocações, devoluções, trocas, oficina e manutenções em ordem cronológica — substitui o diário em texto das planilhas
- [x] **Painel**: card de alocações ativas + seção de trocas em andamento
- [x] 40 testes passando; telas verificadas no navegador

### Etapa 4 — Financeiro (19/07/2026)
- [x] **Cobranças semanais automáticas**: comando `rotina_diaria` (cron) gera o aluguel de cada alocação no dia de vencimento do cliente, recuperando dias perdidos (idempotente); valor considera troca temporária vigente
- [x] **Atrasos e inadimplência**: cobrança vira `Atrasado` e o cliente `Inadimplente` com 1 dia (decisão nº 14); pagamento reverte automaticamente
- [x] **Encargos por atraso**: sugestão automática 5% (≤4 dias) / 10% (acima), sempre editável/zerável antes de aplicar (decisão nº 13); fora da base do DAS
- [x] **Baixa de recebimento com travas**: distribuição entre cobranças com limite por saldo devedor e por valor recebido; totais em tempo real; sobra vira **crédito do cliente** ou reforço de caução; pagamento com crédito valida saldo
- [x] **Notas de débito**: numeração automática em sequência, itens livres (multas viram itens na etapa 5), cobrança única gerada na emissão
- [x] **Caução**: criada automaticamente na alocação com valor acordado; extrato (recebimento, reforço, desconto, devolução); desconto quita cobrança validando os dois saldos
- [x] **Classificação fiscal / Base do DAS**: aluguel → locação (fatura, tributável); resto → pagamentos diversos (ND, fora da base); tela mensal com exportação CSV para a contabilidade (decisões nº 11 e 18)
- [x] **Cobrança judicial**: status próprio para dívidas de ex-clientes (decisão nº 17)
- [x] **Painel**: recebido/a receber na semana, total em atraso, inadimplentes
- [x] 61 testes passando; fluxo completo verificado no navegador

**Pendente do usuário (deploy):** configurar o cron do Railway para rodar `python manage.py rotina_diaria` 1×/dia.

## 🔜 Próximas

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
