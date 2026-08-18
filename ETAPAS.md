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

### Etapa 8 — Hubs e cadastros amigáveis (07/08/2026)
*Implementada com orquestração multi-agente (3 agentes Opus em paralelo) + revisão adversarial (Fable/Opus) que confirmou e corrigiu 15 achados antes do commit.*
- [x] **Hub da Frota** (`/frota/`): cards de veículos com status colorido, categoria, motorista atual (inclusive substituto de troca), valor semanal e mini-alertas (preventivas, FICI); filtros por status/uso/placa; dados dos cards em **queries fixas** (não cresce com a frota — teste de teto de queries)
- [x] **Página do veículo** (`/frota/veiculo/N/`): dados cadastrais completos + atalhos com contagens (linha do tempo, KM, manutenções, multas, sinistros, ficha financeira) + ações (editar, alocar, vender, registrar manutenção)
- [x] **Hub de Clientes** (`/clientes/`): cards com carro atual, telefone com link **WhatsApp** (DDI tratado), saldo devedor/crédito/caução em lote, CNH com alerta; página do cliente com alocações, multas recentes e **condutores autorizados** com cadastro inline
- [x] **Cadastros na plataforma** (saíram do Admin): veículo (novo/editar), cliente, condutor, categorias, fornecedores, **órgãos autuadores** (senha nunca volta no HTML; em branco mantém a atual; páginas com `never_cache`) e **plano de preventivas editável** (itens + intervalos personalizados, com validações)
- [x] **Edição de alocação** (valor/vencimento/franquia — só ativas; encerradas são histórico) e pré-seleção de veículo ao alocar a partir do hub
- [x] **Busca global** na barra do topo (placa ou nome; resultado único redireciona direto)
- [x] **Blindagens da revisão**: `status` do veículo fora do formulário (gerido pelos fluxos — evitava dessincronia e 500), KM atual não diminui pela edição, status "Inadimplente" do cliente só automático, confirmação antes de remover intervalo, IDs adulterados viram 404
- [x] 151 testes passando (+58); telas verificadas no navegador

### Etapa 9 — Fechamento funcional (11/08/2026)
*Revisão adversarial multi-agente (Fable/Opus, 16 agentes) confirmou 11 achados — todos corrigidos com teste de regressão — e refutou 3.*
- [x] **Cobrança automática de excedente de KM** (docs.md §4.8): ao registrar a leitura mensal de um veículo com alocação *Limitado*, o sistema calcula o excedente e gera a cobrança (origem "Excedente de km", prazo de 7 dias), avisando na tela; a rotina diária faz o catch-up (janela de 45 dias); badge do excedente na tela de KM
- [x] **Cálculo justo do excedente** (blindagens da revisão): conta só o km do próprio cliente (base = km de entrega; primeira leitura de carro usado não cobra a vida inteira; km do motorista anterior fora), franquia rateada pelos dias com o carro, **troca temporária** cobre o km rodado no substituto, **acerto final** quando o contrato encerra no meio do período (até o km da devolução), e leitura digitada com atraso vence 7 dias após o lançamento — não nasce vencida nem derruba o cliente para inadimplente
- [x] **"Não cobrar"** (pedido do dono): botão com confirmação na tela de cobranças cancela cobranças automáticas (excedente, encargo, avulsa) sem pagamento aplicado — some do saldo devedor sem apagar o registro; judicial não cancela; cancelar um encargo devolve a sugestão; apagar a cobrança pelo Admin é bloqueado (o caminho é cancelar)
- [x] **Gráfico receita × despesa** dos últimos 6 meses na página de relatórios (Chart.js local; receitas sem caução; proteção Auto Truck recortada pela frota de cada mês, sem reescrever o passado)
- [x] **Assets locais**: Tailwind, HTMX, Alpine e Chart.js vendorizados em `static/vendor/` — funciona sem CDN/internet externa (e `collectstatic` de produção validado)
- [x] **Seed das categorias** confirmadas (decisão nº 15): Gol R$ 650 / Voyage R$ 750 via migração reversível
- [x] 183 testes passando (+32); telas verificadas no navegador (registro com estouro, cancelamento, gráfico)

### Etapa 10 — Auditoria geral do sistema (17/08/2026)
*Auditoria multi-agente do sistema inteiro (38 agentes: 6 lentes — financeira, segurança, integridade de estado, robustez para leigos, refatoração e performance com medição real de queries — cada achado passando por refutador cético ou juiz pragmático). 20 bugs confirmados e 9 melhorias aprovadas, todos executados; 1 achado refutado.*

**Dinheiro (o mais crítico)**
- [x] Mudar o dia de vencimento de uma alocação ativa **duplicava todas as semanas já cobradas** — a geração agora se ancora na última cobrança emitida; mudança de dia vale só para frente
- [x] Devolver o carro no dia do ciclo cobrava uma **semana inteira não usada** (a cobrança é pré-paga); encerrar a alocação cancela as semanas que ainda não começaram e não têm pagamento
- [x] Aluguel quitado por **desconto de caução** ficava fora da base do DAS e da receita do veículo — imposto subdeclarado e ficha de desmobilização errada
- [x] Cliente que paga **volta a Ativo na hora** (antes esperava a rotina do dia seguinte); cobrança judicial mantém o devedor como inadimplente
- [x] Semana com **troca temporária** agora é rateada por dia (valor do substituto × dias, valor da alocação × resto)
- [x] Encerrar alocação com caução retida leva direto ao acerto (a tela prometia e nunca acontecia); sobra de recebimento não reforça caução de contrato encerrado
- [x] Apagar um recebimento no Admin deixava a cobrança "Paga" com saldo devedor para sempre — signals recalculam o status
- [x] **Despesas do mês** passaram a incluir multas absorvidas pela empresa e custos de compra (decisão nº 21); o TOTAL do Excel/CSV fecha com as próprias linhas

**Segurança (antes do deploy)**
- [x] `SECRET_KEY` sem fallback inseguro: com `DEBUG=False` o app **se recusa a subir** sem a variável (a chave também deriva a criptografia das credenciais)
- [x] Senha dos portais de multas **nunca mais volta no HTML** (tela, Admin) e saiu do histórico; `CREDENCIAIS_KEY` própria permite rotacionar a `SECRET_KEY` sem perder as senhas; credencial ilegível avisa em vez de falhar em silêncio
- [x] **Bloqueio de força bruta** no login (6 tentativas por usuário → 15 min, destrava sozinho) e **HSTS** de 30 dias em produção
- [x] Multa em Nota de Débito não pode mais virar advertência por trás da cobrança já emitida

**Consistência e usuário leigo**
- [x] Leitura de KM lançada fora de ordem (mês esquecido) **revalida e reencadeia** o mês seguinte, avisando quando havia excedente cobrado; registrar pendência de mês passado cai no mês certo e volta para o mês filtrado
- [x] Venda bloqueada para carro emprestado como substituto; devolver troca não ressuscita carro vendido; constraint de "uma troca aberta por alocação/substituto" no banco
- [x] Cliente com carro na rua não pode ser marcado inativo; datas incoerentes (término antes do início, devolução antes da retirada) bloqueadas
- [x] Valor em dinheiro aceita o **jeito brasileiro** de digitar (`1.250,00`) — antes gravava 1000× menor em silêncio
- [x] "Judicial" ganhou confirmação, **volta atrás** e passou a poder ser recebida; erro no recebimento **não apaga mais o que foi digitado** e diz qual campo falhou; ND não duplica em duplo clique
- [x] **Paginação** em cobranças, multas e sinistros (antes as listas eram cortadas em silêncio) e botão **"distribuir automaticamente"** no recebimento

**Performance (medida)** — painel 241→38 queries, alertas de preventivas 73→4, plano de preventivas 75→~6, ficha do veículo 148→15, ranking 133→10, lista de alocações 40→4, KM mensal 28→6, tela de cauções e cobranças com totais agregados. 13 testes de teto de queries garantem que nada volta a crescer com o histórico.

**Refatoração** — fonte única para: conjuntos de status de cobrança, parse de dinheiro, período `?mes=`, normalização de placa, regra dos intervalos de preventiva e fichas da frota; `conftest.py` único no lugar da fixture copiada em 22 arquivos; código morto removido.

- [x] **280 testes passando** (+97); `makemigrations --check` e `check --deploy` limpos; telas verificadas no navegador

### Etapa 11 — Fotos, CNH automática, IPVA, WhatsApp e previsão em dias (18/08/2026)
- [x] **Foto do carro e do motorista**: upload no cadastro, exibida nos cards da frota e de clientes e nas fichas; arquivos servidos por rota autenticada (`/midia/…` atrás do login — CNH e fotos nunca viram URL pública); em produção, volume persistente via `MEDIA_ROOT`
- [x] **CNH com foto (frente e verso)** no cadastro do motorista + **leitura automática**: botão "Ler dados da CNH" envia as fotos para a API do Claude (visão + saída estruturada) e preenche nome, CPF, número, categoria e validade **como sugestão** — quem cadastra confere e ajusta antes de salvar; sem `ANTHROPIC_API_KEY` o cadastro funciona normalmente, só sem o preenchimento
- [x] **IPVA e licenciamento** (prometidos no §4.1/§5, decisão nº 21): campos no veículo (ano, valor, vencimento, pago em), alerta de vencimento no painel (some ao pagar), badge pago/em aberto na ficha e despesa do mês do pagamento nos relatórios (tela, export e gráfico)
- [x] **Cobrança pronta no WhatsApp**: botão "💬 cobrar no WhatsApp" nas cobranças devidas abre o wa.me do cliente com a mensagem preenchida (nome, valor, vencimento — variação para atraso com dias — e a `CHAVE_PIX` configurada); nada é enviado automaticamente
- [x] **Previsão das preventivas em dias**: além dos km, cada item mostra "≈ X dias" no ritmo real do carro (média da última leitura mensal) e o alerta antecipa quando faltam ≤ 14 dias — um carro de app a 300 km/dia era avisado só 3 dias antes pela margem de 1.000 km
- [x] Menu em três zonas (logo à esquerda, navegação central, Admin/busca/sair à direita)
- [x] **CNH em PDF** (CNH digital) além de foto, com leitura automática disparada ao escolher o arquivo — e aviso visível na tela quando a `ANTHROPIC_API_KEY` não está configurada
- [x] **Documento do carro (CRLV)** no cadastro do veículo: foto ou PDF, leitura automática preenchendo placa, renavam, chassi, marca/modelo e ano; "ver CRLV" na ficha
- [x] **Contrato de locação** gerado da alocação (partes, veículo, valores, caução, franquia): minuta pronta para imprimir/assinar (PDF pelo navegador), oferecida ao criar a alocação e disponível na lista; dados da empresa via `EMPRESA_*`
- [x] **Checklist de vistoria (entrada/saída)**: o sistema imprime o formulário em branco (itens do carro, km, combustível, notas), o preenchimento é à mão, e a foto do papel preenchido é lida para carregar a vistoria no sistema — validação humana antes de salvar; km da vistoria puxa o odômetro
- [x] **322 testes passando** (+42; toda leitura por API testada com mock); telas verificadas no navegador

## 🎉 MVP completo

As 7 etapas do plano (docs-tecnico.md §5) estão implementadas. Próximos passos fora do código:
1. **Deploy**: criar Postgres no Neon (free) + serviço no Railway apontando para o GitHub; configurar `SECRET_KEY` (**obrigatória**), `DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` e, de preferência, `CREDENCIAIS_KEY` antes de cadastrar credenciais de portais; opcionais `ANTHROPIC_API_KEY` (leitura de CNH), `CHAVE_PIX` (mensagem de cobrança) e volume persistente com `MEDIA_ROOT` para as fotos; cron diário `python manage.py rotina_diaria`.
2. **Carga inicial**: cadastrar frota/clientes reais (ou preparar os "outros dados organizados" — decisão nº 9).
3. **Fase 2 do roadmap** (docs.md §7): anexos, notificações WhatsApp/e-mail, emissão de fatura/ND, importação de planilhas.
