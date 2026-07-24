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

### Etapa 5 — Multas e Sinistros (19/07/2026)
- [x] **Órgãos autuadores**: cadastro com portal, procedimento/documentos e **credenciais criptografadas no banco** (Fernet derivado da SECRET_KEY), ocultas na tela com botão "revelar"
- [x] **Multas**: cliente da alocação preenchido automaticamente por quem estava com o carro na data (considera trocas temporárias), condutor identificado, resultado (advertência/penalidade/dívida ativa...), pagamento (com "pago por"), responsável (cliente/condutor/empresa/**vendedor anterior**)
- [x] **FICI**: prazo limite com alerta no painel (≤7 dias ou vencido), ação "indicar FICI" e **multa NIC** vinculada à original contra a empresa quando o prazo é perdido
- [x] **Repasse via ND**: tela "Gerar ND de multas" seleciona as multas "a cobrar" do cliente e emite ND numerada; recebimento da ND marca as multas como "Recebido"
- [x] **Sinistros**: tipo (colisão/roubo), envolvido, responsabilidade ("culpa 3º"), evento Auto Truck com franquia/cota (zero = franquia gratuita), status
- [x] **Auxílio motorista profissional**: dias parado calculados das manutenções vinculadas; colisão parada **>7 dias** dispara alerta no painel e na tela; fluxo solicitar → receber (decisão nº 10)
- [x] **Manutenção completa**: oficina (cadastro de Fornecedores), entrada/saída (dias parado), origem do custo (evento da proteção × particular), **custo real × valor cobrado** (com diferença), responsável e botão "Gerar repasse" que cria a cobrança do cliente
- [x] 75 testes passando; telas verificadas no navegador

**Nota:** o banco de dev tinha tabelas de um experimento anterior (manutenção/multas/sinistros/financeiro) — foram recriadas do zero pelas migrações; nenhum dado real foi perdido.

### Etapa 6 — Desmobilização (19/07/2026)
- [x] **Compra e venda no veículo**: custos de entrada, mensalidade da proteção ("$ AT"), e venda (data, valor, comprador, custos, KM) — bloqueada com alocação ativa; status → Vendido com histórico preservado; crédito da venda fora da base do DAS
- [x] **Ficha financeira por veículo** (`/frota/veiculo/N/ficha/`): investido (compra + entrada), receitas (aluguéis + repasses + auxílios recebidos), despesas (manutenções, franquias, multas absorvidas, proteção estimada pela mensalidade), resultado operacional, **% do investimento recuperado** e resultado final após a venda — tudo calculado dos lançamentos existentes, sem redigitação
- [x] **Recomendação explicável** (decisão nº 19): critérios configuráveis — % recuperado ≥75% (janela ~70–80% dos donos), custo manutenção/km 50% acima da média da frota, >20 dias parado (6m), ≥2 esporádicas pesadas (6m) — geram nível 🟢 Manter / 🟡 Observar / 🟠 Preparar / 🔴 Vender **sempre listando os motivos**
- [x] **Ranking da frota** (`/frota/desmobilizacao/`): piores primeiro, com indicadores e "registrar venda"; seção "Frota vendida" com resultado final por unidade
- [x] **Painel**: seção "Candidatos à desmobilização" (🟠/🔴) com motivos
- [x] 85 testes passando; telas verificadas no navegador

### Etapa 7 — Painel consolidado e relatórios (24/07/2026)
- [x] **Painel consolidado** (docs.md §5 completo): taxa de ocupação da frota, **vigências e documentos a vencer em 30 dias** (rastreador, garantia da bateria, CNHs de clientes ativos — vencidos em destaque), NDs em aberto, além de tudo que já existia (financeiro, FICI, auxílios, candidatos à desmobilização, preventivas, KM pendentes, trocas)
- [x] **Central de relatórios** (`/relatorios/`) com seletor de mês:
  - **Receitas do mês** nas 3 classes fiscais (locação/base do DAS, diversos, caução) + auxílios e vendas (outros créditos)
  - **Despesas do mês** (pedido da Luciana): manutenções detalhadas, franquias de eventos, mensalidades da proteção, custos de venda
  - **Recebíveis em aberto** por cliente (com destaque judicial) e cauções retidas
  - **Resumo da frota** (resultado, % recuperado e recomendação por veículo)
- [x] **Exportação Excel (openpyxl) e CSV** em todos os relatórios, para envio à contabilidade (decisão nº 18) — PDF fica para depois se necessário
- [x] 93 testes passando; painel e relatórios verificados no navegador

## 🎉 MVP completo

As 7 etapas do plano (docs-tecnico.md §5) estão implementadas. Próximos passos fora do código:
1. **Deploy**: criar Postgres no Neon (free) + serviço no Railway apontando para o GitHub; configurar `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`; cron diário `python manage.py rotina_diaria`.
2. **Carga inicial**: cadastrar frota/clientes reais (ou preparar os "outros dados organizados" — decisão nº 9).
3. **Fase 2 do roadmap** (docs.md §7): anexos, notificações WhatsApp/e-mail, emissão de fatura/ND, importação de planilhas.
