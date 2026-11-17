"""Etapa 18c — frota: sobras da etapa 18 e refatoração com o comportamento travado.

Ficha do carro separa o aluguel do excedente/encargo, vendidos em cartões no
celular, atalho "Dar carro substituto", números formatados na retaguarda e
alvo de toque do <summary> do ranking.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.financeiro import services as financeiro
from apps.financeiro.models import Cobranca
from apps.frota import desmobilizacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(
        placa="ETA8C18",
        marca_modelo="Onix",
        km_compra=40_000,
        km_atual=40_000,
        data_aquisicao=HOJE - timedelta(days=120),
        valor_compra=Decimal("50000.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


@pytest.fixture
def alocacao(carro, cliente):
    return Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=30),
        valor_semanal=Decimal("650.00"),
        km_entrega=40_000,
    )


def _cobranca(alocacao, origem, valor):
    return Cobranca.objects.create(
        cliente=alocacao.cliente,
        alocacao=alocacao,
        origem=origem,
        descricao=f"Teste {origem}",
        valor=Decimal(valor),
        vencimento=HOJE - timedelta(days=3),
    )


def _pagar(cobranca):
    financeiro.registrar_recebimento(
        cobranca.cliente, HOJE, cobranca.valor, "pix", [(cobranca, cobranca.valor)]
    )


# --- (1) Ficha: "Aluguéis recebidos" só com o aluguel semanal ---------------------------


def test_ficha_separa_aluguel_de_excedente_e_encargo_sem_mudar_o_total(alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "120.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("185.00")
    # o total da receita continua o mesmo de antes (tudo do contrato entra)
    assert ficha.receita_total == Decimal("835.00")


def test_ficha_separa_desconto_de_caucao_pela_origem(alocacao, carro):
    caucao = financeiro.abrir_caucao(
        alocacao, valor_recebido=Decimal("1000.00"), data=HOJE - timedelta(days=30)
    )
    aluguel = _cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00")
    excedente = _cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "100.00")
    financeiro.descontar_da_caucao(caucao, aluguel, Decimal("650.00"), HOJE)
    financeiro.descontar_da_caucao(caucao, excedente, Decimal("100.00"), HOJE)
    ficha = desmobilizacao.montar_ficha(carro)
    assert ficha.receita_aluguel == Decimal("650.00")
    assert ficha.receita_extras_locacao == Decimal("100.00")
    assert ficha.receita_total == Decimal("750.00")


def test_tela_da_ficha_mostra_a_linha_do_excedente_so_quando_ha(usuario_logado, alocacao, carro):
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    url = reverse("frota:ficha", args=[carro.pk])
    resposta = usuario_logado.get(url)
    assert "Excedente de km, encargos" not in resposta.content.decode()
    _pagar(_cobranca(alocacao, Cobranca.Origem.ENCARGO, "65.00"))
    html = usuario_logado.get(url).content.decode()
    assert "Excedente de km, encargos e outras cobranças do contrato" in html
    assert "+ R$ 65,00" in html
    assert "+ R$ 650,00" in html


# --- (2) /frota/desmobilizacao/: vendidos em cartões no celular -------------------------


def test_vendidos_viram_cartoes_no_celular_com_o_resultado_a_vista(usuario_logado):
    Veiculo.objects.create(
        placa="VND8C18",
        marca_modelo="HB20",
        data_aquisicao=HOJE - timedelta(days=400),
        valor_compra=Decimal("40000.00"),
        status=Veiculo.Status.VENDIDO,
        data_venda=HOJE - timedelta(days=5),
        valor_venda=Decimal("45000.00"),
    )
    html = usuario_logado.get(reverse("frota:ranking")).content.decode()
    # o cartão (só no celular) mostra o resultado; a tabela só aparece da tela média em diante
    assert '<ul class="space-y-3 md:hidden" aria-label="Carros já vendidos">' in html
    assert '<div class="hidden overflow-x-auto rounded-lg bg-white shadow md:block">' in html
    cartao = html.split('aria-label="Carros já vendidos">')[1].split("</ul>")[0]
    assert "VND8C18" in cartao
    assert "Resultado final" in cartao
    assert "R$ 5.000,00" in cartao  # 45.000 − 40.000
    assert "investido R$ 40.000,00" in cartao


# --- (3) Atalho "Dar carro substituto" no detalhe do carro ------------------------------


def _na_oficina(carro):
    from apps.manutencao.models import Manutencao

    return Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.ESPORADICA,
        data=HOJE - timedelta(days=2),
        data_entrada=HOJE - timedelta(days=2),
        descricao="Motor",
    )


def test_detalhe_mostra_dar_carro_substituto_com_carro_alocado_na_oficina(
    usuario_logado, alocacao, carro
):
    url = reverse("frota:detalhe", args=[carro.pk])
    assert "Dar carro substituto" not in usuario_logado.get(url).content.decode()
    _na_oficina(carro)
    html = usuario_logado.get(url).content.decode()
    assert "Dar carro substituto" in html
    link = reverse("alocacoes:troca_nova", args=[alocacao.pk])
    assert (
        f"{link}?motivo=Manuten%C3%A7%C3%A3o%20do%20carro%20principal&amp;voltar="
        f"/frota/veiculo/{carro.pk}/" in html
    )


def test_detalhe_sem_atalho_quando_nao_ha_alocacao_ou_ja_tem_substituto(
    usuario_logado, alocacao, carro
):
    from apps.alocacoes.models import TrocaTemporaria

    _na_oficina(carro)
    reserva = Veiculo.objects.create(placa="RES8C18", marca_modelo="Gol", km_atual=1000)
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=reserva,
        data_retirada=HOJE - timedelta(days=1),
        km_retirada=1000,
    )
    url = reverse("frota:detalhe", args=[carro.pk])
    assert "Dar carro substituto" not in usuario_logado.get(url).content.decode()
    # carro sem contrato, só na oficina: não há a quem dar substituto
    sozinho = Veiculo.objects.create(placa="SOL8C18", marca_modelo="Gol")
    _na_oficina(sozinho)
    html = usuario_logado.get(reverse("frota:detalhe", args=[sozinho.pk])).content.decode()
    assert "Dar carro substituto" not in html


# --- (4) Retaguarda: KM e R$ formatados nas listas --------------------------------------


def test_retaguarda_formata_km_e_reais_nas_listas(admin_client, carro):
    from apps.frota.models import Categoria, PagamentoIpva

    carro.km_atual = 48_900
    carro.mensalidade_protecao = Decimal("1234.50")
    carro.save()
    PagamentoIpva.objects.create(veiculo=carro, ano=HOJE.year, valor=Decimal("1890.75"))
    Categoria.objects.create(nome="Hatch 8C", valor_semanal_referencia=Decimal("1650.00"))

    html = admin_client.get("/admin/frota/veiculo/").content.decode()
    assert "48.900" in html and "R$ 1.234,50" in html
    assert "R$ 1.890,75" in admin_client.get("/admin/frota/pagamentoipva/").content.decode()
    assert "R$ 1.650,00" in admin_client.get("/admin/frota/categoria/").content.decode()
    # a coluna continua ordenável pelo campo do banco
    assert admin_client.get("/admin/frota/veiculo/?o=7").status_code == 200


# --- (5) Ranking: <summary> com alvo de toque de 44px -----------------------------------


def test_summary_do_ranking_tem_alvo_de_toque_de_44px(usuario_logado):
    html = usuario_logado.get(reverse("frota:ranking")).content.decode()
    assert '<summary class="-my-3 cursor-pointer py-3 font-semibold">' in html


# === Refatoração (comportamento travado antes de mexer) =================================

DIA_FIXO = HOJE - timedelta(days=90)


def _vender(carro, **extras):
    dados = {"data": HOJE, "valor": Decimal("30000.00"), "comprador": "João"}
    dados.update(extras)
    return desmobilizacao.registrar_venda(carro, **dados)


# --- R1: venda com a cópia velha do carro (dois toques, duas abas) ----------------------


def test_venda_com_copia_velha_nao_sobrescreve_venda_ja_gravada(carro):
    primeira = Veiculo.objects.get(pk=carro.pk)
    segunda = Veiculo.objects.get(pk=carro.pk)
    _vender(primeira)
    with pytest.raises(ValidationError) as erro:
        _vender(segunda, valor=Decimal("3000.00"), comprador="X")
    assert "Veículo já vendido." in erro.value.messages
    carro.refresh_from_db()
    assert carro.valor_venda == Decimal("30000.00")
    assert carro.comprador == "João"
    assert carro.history.filter(status=Veiculo.Status.VENDIDO).count() == 1


def test_venda_respeita_o_km_e_os_dados_gravados_depois_de_abrir_a_tela(carro):
    copia = Veiculo.objects.get(pk=carro.pk)  # tela aberta com 40.000 km
    Veiculo.objects.filter(pk=carro.pk).update(km_atual=45_000, observacoes="Pneu novo")
    with pytest.raises(ValidationError) as erro:
        _vender(copia, km=42_000)
    assert any("KM da venda menor" in m for m in erro.value.messages)
    _vender(copia)
    carro.refresh_from_db()
    assert carro.status == Veiculo.Status.VENDIDO
    assert carro.km_atual == 45_000  # não voltou para o KM da tela velha
    assert carro.observacoes == "Pneu novo"  # o que foi gravado nesse meio-tempo fica


def test_venda_recusada_nao_mexe_no_carro_de_quem_chamou(carro):
    with pytest.raises(ValidationError):
        _vender(carro, valor=Decimal("0"))
    assert carro.data_venda is None and carro.valor_venda is None
    assert carro.status != Veiculo.Status.VENDIDO


def test_venda_aceita_atualiza_o_carro_de_quem_chamou(carro):
    _vender(carro, km=41_000, custos=Decimal("500.00"))
    # quem chamou (a tela, os testes) continua vendo o carro vendido sem recarregar
    assert carro.status == Veiculo.Status.VENDIDO
    assert carro.data_venda == HOJE and carro.valor_venda == Decimal("30000.00")
    assert carro.km_atual == 41_000 and carro.km_venda == 41_000
    assert carro.custos_venda == Decimal("500.00") and carro.comprador == "João"


def test_tela_de_venda_com_carro_ja_vendido_em_outra_aba_mostra_que_foi_vendido(
    usuario_logado, carro
):
    url = reverse("frota:vender", args=[carro.pk])
    dados = {"data": HOJE.isoformat(), "valor": "30.000,00"}
    assert usuario_logado.post(url, dados).status_code == 302
    html = usuario_logado.post(url, dados | {"valor": "3.000,00"}).content.decode()
    assert "Venda não registrada: Veículo já vendido." in html
    assert f"{carro.placa} já foi vendido." in html
    carro.refresh_from_db()
    assert carro.valor_venda == Decimal("30000.00")


# --- R2: tela "Quando vender" com as fichas num lote só ---------------------------------


def _frota_mista():
    from apps.frota.test_revisao_etapa8 import _frota_de_teste

    veiculos = _frota_de_teste(8)
    for indice, veiculo in enumerate(veiculos[:3]):
        Veiculo.objects.filter(pk=veiculo.pk).update(
            status=Veiculo.Status.VENDIDO,
            data_venda=HOJE - timedelta(days=indice * 3),
            valor_venda=Decimal("30000.00") + indice,
        )
    return veiculos


def _numeros(ficha):
    return {k: v for k, v in vars(ficha).items()} | {"resultado_final": ficha.resultado_final}


def test_tela_quando_vender_mostra_as_mesmas_fichas_de_antes(usuario_logado, db):
    _frota_mista()
    resposta = usuario_logado.get(reverse("frota:ranking"))
    fichas, media = desmobilizacao.ranking_da_frota()
    vendidos = Veiculo.objects.filter(status=Veiculo.Status.VENDIDO).order_by("-data_venda")
    esperado_vendidos = desmobilizacao.montar_fichas_em_lote(vendidos)
    assert resposta.context["media"] == media
    assert [_numeros(f) for f in resposta.context["fichas"]] == [_numeros(f) for f in fichas]
    assert [_numeros(f) for f in resposta.context["fichas_vendidos"]] == [
        _numeros(f) for f in esperado_vendidos
    ]
    # mais recente primeiro
    datas = [f.veiculo.data_venda for f in resposta.context["fichas_vendidos"]]
    assert datas == sorted(datas, reverse=True)


def test_tela_quando_vender_monta_as_fichas_num_lote_so(
    usuario_logado, django_assert_max_num_queries, db
):
    _frota_mista()
    # sessão + usuário + 2 listas de carros + 10 da ficha em lote (antes: 24)
    with django_assert_max_num_queries(15):
        assert usuario_logado.get(reverse("frota:ranking")).status_code == 200


# --- R3: detalhe do carro com as contagens na consulta do próprio carro -----------------


def test_detalhe_conta_os_registros_de_cada_carro_sem_misturar(usuario_logado, carro):
    from apps.km.models import RegistroKm
    from apps.manutencao.models import Manutencao
    from apps.multas.models import Multa
    from apps.sinistros.models import Sinistro

    outro = Veiculo.objects.create(placa="OUT8C18", marca_modelo="Gol", km_atual=1000)
    for mes in (1, 2):
        RegistroKm.objects.create(
            veiculo=carro,
            mes_referencia=HOJE.replace(day=1) - timedelta(days=31 * mes),
            data_leitura=HOJE - timedelta(days=31 * mes),
            km=40_000 + mes * 1000,
        )
    for indice in range(3):
        Manutencao.objects.create(
            veiculo=carro,
            tipo="corretiva",
            data=HOJE - timedelta(days=10 + indice),
            descricao="Serviço",
            data_entrada=HOJE - timedelta(days=10 + indice),
            data_saida=HOJE - timedelta(days=10 + indice),
        )
    Manutencao.objects.create(veiculo=outro, tipo="corretiva", data=HOJE, descricao="Outro")
    Multa.objects.create(veiculo=carro, data_infracao=HOJE, valor=Decimal("130"))
    Multa.objects.create(veiculo=outro, data_infracao=HOJE, valor=Decimal("130"))
    Multa.objects.create(veiculo=outro, data_infracao=HOJE, valor=Decimal("130"))
    Sinistro.objects.create(veiculo=carro, data=HOJE, envolvido="terceiro")

    contexto = usuario_logado.get(reverse("frota:detalhe", args=[carro.pk])).context
    assert (contexto["total_km"], contexto["total_manutencoes"]) == (2, 3)
    assert (contexto["total_multas"], contexto["total_sinistros"]) == (1, 1)
    vazio = usuario_logado.get(reverse("frota:detalhe", args=[outro.pk])).context
    assert (vazio["total_km"], vazio["total_manutencoes"]) == (0, 1)
    assert (vazio["total_multas"], vazio["total_sinistros"]) == (2, 0)


# --- R4: a ficha usa o "hoje" recebido também nos dias parado ---------------------------


def test_ficha_com_hoje_informado_conta_os_dias_parado_ate_esse_dia(carro):
    from apps.manutencao.models import Manutencao

    Manutencao.objects.create(
        veiculo=carro,
        tipo="esporadica",
        data=DIA_FIXO - timedelta(days=5),
        data_entrada=DIA_FIXO - timedelta(days=5),
        descricao="Motor",
    )
    assert desmobilizacao.montar_ficha(carro, hoje=DIA_FIXO).dias_parado_6m == 5
    assert desmobilizacao.montar_ficha(carro).dias_parado_6m == 95


# --- R5: conserto pago pela cota da proteção — uma regra só -----------------------------


def test_media_da_ficha_e_do_ranking_batem_com_conserto_pela_cota(db):
    from apps.frota.test_revisao_etapa8 import HOJE as DIA
    from apps.frota.test_revisao_etapa8 import _frota_de_teste
    from apps.manutencao.models import Manutencao
    from apps.sinistros.models import Sinistro

    veiculos = _frota_de_teste(4)
    com_franquia = Sinistro.objects.create(
        veiculo=veiculos[0], data=DIA, envolvido="terceiro", franquia_valor=Decimal("800")
    )
    sem_franquia = Sinistro.objects.create(veiculo=veiculos[1], data=DIA, envolvido="terceiro")
    casos = [
        (veiculos[0], Manutencao.OrigemCusto.PROTECAO, com_franquia, Decimal("5000")),  # cota
        (veiculos[1], Manutencao.OrigemCusto.PROTECAO, sem_franquia, Decimal("700")),
        (veiculos[2], Manutencao.OrigemCusto.PARTICULAR, None, Decimal("400")),
        (veiculos[3], Manutencao.OrigemCusto.PROTECAO, None, None),  # sem custo
    ]
    for veiculo, origem, sinistro, custo in casos:
        Manutencao.objects.create(
            veiculo=veiculo,
            tipo="corretiva",
            data=DIA - timedelta(days=5),
            descricao="Conserto",
            origem_custo=origem,
            sinistro=sinistro,
            custo_real=custo,
        )
    fichas, media = desmobilizacao.ranking_da_frota(hoje=DIA)
    custos = {f.veiculo.pk: f.custo_manutencao_6m for f in fichas}
    # 300 da manutenção de cada carro + o conserto que não foi pago pela cota
    assert custos[veiculos[0].pk] == Decimal("300")
    assert custos[veiculos[1].pk] == Decimal("1000")
    assert custos[veiculos[2].pk] == Decimal("700")
    assert custos[veiculos[3].pk] == Decimal("300")
    assert desmobilizacao.media_custo_km_frota(hoje=DIA) == media
    # e a despesa total da ficha usa o mesmo critério
    total = {f.veiculo.pk: f.despesa_manutencao for f in fichas}
    assert total[veiculos[0].pk] == Decimal("300")
    assert total[veiculos[1].pk] == Decimal("1000")


# === Revisão adversarial =================================================================


def test_conserto_e_franquia_de_outro_carro_contam_no_carro_consertado(alocacao, carro, cliente):
    """Repasse apontando para a alocação de outro carro (ex.: substituto batido,
    cobrança ligada ao contrato no Admin): a receita fica no carro que teve a
    despesa, uma vez só — nunca no carro do contrato."""
    from apps.manutencao.models import Manutencao
    from apps.sinistros.models import Sinistro

    substituto = Veiculo.objects.create(placa="SUB8C18", marca_modelo="Gol", km_atual=1000)
    repasse = _cobranca(alocacao, Cobranca.Origem.REPASSE_MANUTENCAO, "300.00")
    Manutencao.objects.create(
        veiculo=substituto,
        tipo="corretiva",
        data=HOJE - timedelta(days=4),
        descricao="Para-choque",
        custo_real=Decimal("300.00"),
        cobranca_repasse=repasse,
    )
    franquia = _cobranca(alocacao, Cobranca.Origem.REPASSE_SINISTRO, "800.00")
    Sinistro.objects.create(
        veiculo=substituto,
        data=HOJE - timedelta(days=4),
        envolvido="terceiro",
        cliente=cliente,
        franquia_valor=Decimal("800.00"),
        cobranca_franquia=franquia,
    )
    _pagar(repasse)
    caucao = financeiro.abrir_caucao(
        alocacao, valor_recebido=Decimal("1000.00"), data=HOJE - timedelta(days=30)
    )
    financeiro.descontar_da_caucao(caucao, franquia, Decimal("800.00"), HOJE)

    do_contrato, do_substituto = desmobilizacao.montar_fichas_em_lote([carro, substituto])
    assert do_contrato.receita_total == Decimal("0")
    assert do_substituto.receita_repasses == Decimal("1100.00")
    assert do_substituto.receita_total == Decimal("1100.00")


def test_atalho_dar_carro_substituto_abre_a_troca_com_o_motivo_preenchido(
    usuario_logado, alocacao, carro
):
    _na_oficina(carro)
    detalhe = reverse("frota:detalhe", args=[carro.pk])
    html = usuario_logado.get(detalhe).content.decode()
    link = html.split("Dar carro substituto</a>")[0].rsplit('<a href="', 1)[1].split('"')[0]
    resposta = usuario_logado.get(link.replace("&amp;", "&"))
    assert resposta.status_code == 200
    assert resposta.context["form"].initial["motivo"] == "Manutenção do carro principal"
    assert resposta.context["voltar"] == detalhe


def test_valores_da_composicao_da_ficha_nao_quebram_linha_no_celular(
    usuario_logado, alocacao, carro
):
    # 375px: "+ R$" numa linha e "650,00" na outra confundia a leitura
    _pagar(_cobranca(alocacao, Cobranca.Origem.ALUGUEL, "650.00"))
    _pagar(_cobranca(alocacao, Cobranca.Origem.EXCEDENTE_KM, "120.00"))
    html = usuario_logado.get(reverse("frota:ficha", args=[carro.pk])).content.decode()
    composicao = html.split("Composição")[1].split("</table>")[0]
    valores = composicao.count('<td class="whitespace-nowrap py-1.5 pl-3 text-right')
    assert valores == 10  # 4 receitas + 5 despesas + resultado operacional
    assert '<td class="py-1.5 text-right text-green-700">' not in composicao
    assert '<td class="py-1.5 text-right text-red-700">−' not in composicao


# --- Regressão no navegador: custos da venda à vista na composição da ficha ------------


def test_ficha_de_vendido_mostra_a_conta_da_venda_com_os_custos(usuario_logado, carro):
    # vendeu por 38.500 com 500 de custos (investido 50.000): sem a linha dos 500,
    # quem confere fazia 38.500 − 50.000 e achava que o sistema errou
    _vender(carro, valor=Decimal("38500.00"), custos=Decimal("500.00"))
    html = usuario_logado.get(reverse("frota:ficha", args=[carro.pk])).content.decode()
    composicao = html.split("Composição")[1].split("</table>")[0]
    assert "Venda do carro" in composicao and "+ R$ 38.500,00" in composicao
    assert "Custos da venda" in composicao and "− R$ 500,00" in composicao
    assert "Investido (compra + entrada)" in composicao and "− R$ 50.000,00" in composicao
    assert "−R$ 12.000,00" in composicao or "-R$ 12.000,00" in composicao
    # celular: o cartão do vendido também mostra os custos da venda
    ranking = usuario_logado.get(reverse("frota:ranking")).content.decode()
    cartao = ranking.split('aria-label="Carros já vendidos">')[1].split("</ul>")[0]
    assert "custos da venda R$ 500,00" in cartao


def test_ficha_de_vendido_sem_custos_nao_mostra_linha_vazia(usuario_logado, carro):
    _vender(carro)
    html = usuario_logado.get(reverse("frota:ficha", args=[carro.pk])).content.decode()
    composicao = html.split("Composição")[1].split("</table>")[0]
    assert "Venda do carro" in composicao
    assert "Custos da venda" not in composicao


def test_ranking_no_computador_mostra_os_custos_da_venda(usuario_logado, carro):
    # a tabela do computador também precisa dos custos para fechar a conta do vendido
    _vender(carro, valor=Decimal("38500.00"), custos=Decimal("500.00"))
    html = usuario_logado.get(reverse("frota:ranking")).content.decode()
    tabela = html.split('aria-label="Carros já vendidos">')[1].split("</ul>")[1]
    tabela = tabela.split("</table>")[0]
    assert "custos da venda R$ 500,00" in tabela
    assert "-R$ 12.000,00" in tabela


def test_vendido_sem_valor_da_venda_nao_derruba_e_nao_inventa_resultado(usuario_logado, carro):
    # dado antigo: status Vendido pelo Admin, com data mas sem o valor da venda
    Veiculo.objects.filter(pk=carro.pk).update(status=Veiculo.Status.VENDIDO, data_venda=HOJE)
    ranking = usuario_logado.get(reverse("frota:ranking"))
    assert ranking.status_code == 200
    cartao = ranking.content.decode().split('aria-label="Carros já vendidos">')[1]
    cartao = cartao.split("</ul>")[0]
    # sem valor, o resultado é "—" em cinza, não um vermelho de prejuízo
    assert 'text-gray-500">—</div>' in cartao
    ficha = usuario_logado.get(reverse("frota:ficha", args=[carro.pk]))
    assert ficha.status_code == 200
    composicao = ficha.content.decode().split("Composição")[1].split("</table>")[0]
    assert "Venda do carro" not in composicao and "Resultado final" not in composicao
