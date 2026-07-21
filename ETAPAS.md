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
- [x] **Cobranças** (`/financeiro/cobrancas/`): origens (aluguel, ND, manutenção, sinistro, excedente km, encargo), status Pendente/Parcial/Pago/Atrasado/Judicial, saldo calculado
- [x] **Geração automática de aluguel semanal** no dia de vencimento do cliente (idempotente), com valor ajustado por troca temporária
- [x] **Baixa de recebimento** (`/financeiro/baixa/`): distribuição automática (mais antiga→nova) ou manual, **totais em tempo real** e **travas** (nenhuma parcela acima do saldo; total ≤ recebido; sobra vira crédito) — botão só habilita quando válido
- [x] **Rotina diária** (`manage.py rotina_diaria`, cron): gera cobranças e marca atraso/inadimplência (1 dia); cliente volta a ativo ao quitar
- [x] **Encargos por atraso** sugeridos (5% até 4 dias, 10% acima), ajustáveis ou zeráveis
- [x] **Notas de débito** com numeração automática e itens; **caução** opcional com extrato (reforço/desconto/devolução) e trava de saldo
- [x] **Classificação fiscal**: só aluguel entra na base do DAS; relatório mensal (`/financeiro/das/`) + **exportação CSV** para a contabilidade
- [x] **Cobrança judicial**: status para dívidas de ex-clientes
- [x] Painel: total a receber + inadimplentes
- [x] 66 testes; tela de baixa verificada no navegador (distribuição, travas e botão) — corrigido bug de vírgula decimal (pt-BR) que quebrava o JS

**Nota:** o acerto automático de caução no encerramento da alocação e o abatimento de débito direto da caução ficaram como melhoria simples para a etapa 5/6 (a caução já existe e é movimentável).

### Etapa 5 — Multas e Sinistros (19/07/2026)
- [x] **Órgãos autuadores** (cadastro): portal, login e **senha mascarada** na interface (widget PasswordInput no admin), procedimento e documentos exigidos
- [x] **Fornecedores/oficinas** (cadastro)
- [x] **Multas** (`/multas/`): código, AIT, processamento, órgão, valor, pontos, resultado; **cliente da alocação preenchido automaticamente** pela data (via `cliente_vigente`, considerando trocas)
- [x] **FICI** com prazo + **alerta no painel** de indicações a vencer (evita multa NIC); **multa NIC** vinculável à multa original
- [x] **Emitir ND**: agrupa as multas "a cobrar" de um cliente, gera a cobrança no financeiro e marca as multas como "Incluída em ND"
- [x] **Sinistros** (`/sinistros/`): tipo (colisão/roubo/outro), envolvido, responsabilidade, evento Auto Truck + franquia; **motorista preenchido automaticamente**
- [x] **Auxílio motorista profissional**: detecção automática de colisão parada > 7 dias (via manutenção com data de entrada), registro e acompanhamento (a solicitar/solicitado/recebido)
- [x] **Manutenção completa**: fornecedor, entrada/saída (**dias parado**), origem do custo, **custo real × valor cobrado** (com resultado), responsável e status de repasse
- [x] Painel: alertas de FICI a vencer, auxílios a solicitar e sinistros abertos
- [x] 86 testes; multas, ND e sinistros verificados no navegador — corrigido formato do valor na mensagem de ND

**Nota:** credenciais dos órgãos são mascaradas na UI mas gravadas em texto no banco; criptografia em repouso (django-fernet) fica como endurecimento futuro. O repasse de manutenção/sinistro→cobrança (fluxo A_COBRAR) usa a mesma emissão de ND das multas quando aplicável.

## 🔜 Próximas

### Etapa 6 — Desmobilização
- Ficha financeira por veículo (% do investimento recuperado)
- Indicadores e recomendação de venda, ranking da frota

### Etapa 7 — Painel e relatórios
- Painel consolidado com todos os alertas
- Relatórios + exportação para contabilidade (Excel/CSV/PDF)
