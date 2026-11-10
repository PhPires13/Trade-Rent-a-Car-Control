"""Etapa 18b — frota: achados dos testes no navegador.

Venda antes do último lançamento e KM absurdo, IPVA fora do período em que o
carro era da empresa, ficha aberta antes de uma leitura de KM, observações em
várias linhas na edição em linha, mensagem repetida no cadastro e a ordem da
linha do tempo do carro.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict
from django.utils import timezone

from apps.alocacoes.models import Alocacao, TrocaTemporaria, Vistoria
from apps.alocacoes.services import linha_do_tempo
from apps.frota import desmobilizacao
from apps.frota.models import Categoria, Fornecedor, PagamentoIpva, Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import Manutencao
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(
        placa="TST2E22",
        marca_modelo="Gol",
        km_compra=30_000,
        km_atual=30_000,
        data_aquisicao=HOJE - timedelta(days=200),
        valor_compra=Decimal("40000.00"),
    )


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Arlen", cpf_cnpj="111.222.333-44")


def _leitura(carro, data, km):
    return RegistroKm.objects.create(
        veiculo=carro, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )


# --- Venda: data antes do último lançamento e KM absurdo -------------------------------


def test_venda_antes_da_ultima_leitura_de_km_e_recusada(carro):
    leitura = HOJE - timedelta(days=5)
    _leitura(carro, leitura, 31_000)
    carro.refresh_from_db()
    with pytest.raises(ValidationError) as erro:
        desmobilizacao.registrar_venda(carro, HOJE - timedelta(days=100), Decimal("35000"))
    assert (
        f"O carro tem leitura mensal registrada em {leitura:%d/%m/%Y} — "
        "a venda não pode ser antes disso." in erro.value.messages
    )
    carro.refresh_from_db()
    assert carro.status != Veiculo.Status.VENDIDO


def test_venda_antes_do_fim_da_alocacao_e_recusada(carro, cliente):
    alocacao = Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=60),
        valor_semanal=Decimal("650.00"),
        km_entrega=30_000,
    )
    fim = HOJE - timedelta(days=10)
    alocacao.encerrar(data_termino=fim, km_devolucao=34_000)
    carro.refresh_from_db()
    with pytest.raises(ValidationError) as erro:
        desmobilizacao.registrar_venda(carro, fim - timedelta(days=1), Decimal("35000"))
    assert any(f"registrada em {fim:%d/%m/%Y}" in m for m in erro.value.messages)
    # no mesmo dia da devolução pode
    desmobilizacao.registrar_venda(carro, fim, Decimal("35000"))
    carro.refresh_from_db()
    assert carro.status == Veiculo.Status.VENDIDO


def test_venda_antes_da_saida_da_oficina_e_recusada(carro):
    saida = HOJE - timedelta(days=3)
    Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=8),
        descricao="Freio",
        data_entrada=HOJE - timedelta(days=8),
        data_saida=saida,
    )
    with pytest.raises(ValidationError) as erro:
        desmobilizacao.registrar_venda(carro, HOJE - timedelta(days=5), Decimal("35000"))
    assert any("saída da oficina" in m for m in erro.value.messages)


def test_km_absurdo_na_venda_pede_confirmacao(carro):
    _leitura(carro, HOJE - timedelta(days=5), 31_000)
    carro.refresh_from_db()
    with pytest.raises(ValidationError) as erro:
        desmobilizacao.registrar_venda(carro, HOJE, Decimal("35000"), km=310_000)
    assert erro.value.error_list[0].code == "km_suspeito"
    assert "Confirmo o KM" in erro.value.messages[0]
    carro.refresh_from_db()
    assert carro.km_atual == 31_000
    assert carro.status != Veiculo.Status.VENDIDO
    # KM normal passa sem confirmação; o absurdo passa só confirmado
    desmobilizacao.registrar_venda(carro, HOJE, Decimal("35000"), km=310_000, confirmar_km=True)
    carro.refresh_from_db()
    assert carro.km_atual == 310_000


def test_km_normal_na_venda_nao_pede_confirmacao(carro):
    _leitura(carro, HOJE - timedelta(days=5), 31_000)
    carro.refresh_from_db()
    desmobilizacao.registrar_venda(carro, HOJE, Decimal("35000"), km=31_500)
    carro.refresh_from_db()
    assert carro.status == Veiculo.Status.VENDIDO
    assert carro.km_atual == 31_500


def test_tela_de_venda_mostra_caixa_confirmo_o_km(usuario_logado, carro):
    _leitura(carro, HOJE - timedelta(days=5), 31_000)
    url = f"/frota/veiculo/{carro.pk}/vender/"
    dados = {"data": HOJE.isoformat(), "valor": "35.000,00", "km": "310.000"}
    resposta = usuario_logado.post(url, dados)
    assert resposta.status_code == 200
    assert 'name="confirmar_km"' in resposta.content.decode()
    carro.refresh_from_db()
    assert carro.status != Veiculo.Status.VENDIDO
    resposta = usuario_logado.post(url, {**dados, "confirmar_km": "1"})
    assert resposta.status_code == 302
    carro.refresh_from_db()
    assert carro.km_atual == 310_000


def test_km_da_venda_e_campo_de_texto_e_aceita_ponto_de_milhar(usuario_logado, carro):
    url = f"/frota/veiculo/{carro.pk}/vender/"
    pagina = usuario_logado.get(url).content.decode()
    assert 'id="venda-km" type="text"' in pagina
    assert 'inputmode="numeric"' in pagina
    assert 'name="confirmar_km"' not in pagina
    resposta = usuario_logado.post(
        url, {"data": HOJE.isoformat(), "valor": "35.000,00", "km": "31.500"}
    )
    assert resposta.status_code == 302
    carro.refresh_from_db()
    assert carro.km_atual == 31_500


# --- IPVA fora do período em que o carro era da empresa -------------------------------


def _post_ipva(client, carro, pago_em, existentes=0):
    dados = {
        "pagamentos_ipva-TOTAL_FORMS": "1",
        "pagamentos_ipva-INITIAL_FORMS": "0",
        "pagamentos_ipva-MIN_NUM_FORMS": "0",
        "pagamentos_ipva-MAX_NUM_FORMS": "1000",
        "pagamentos_ipva-0-ano": str(pago_em.year),
        "pagamentos_ipva-0-parcela": "1",
        "pagamentos_ipva-0-valor": "1.200,00",
        "pagamentos_ipva-0-vencimento": "",
        "pagamentos_ipva-0-pago_em": pago_em.isoformat(),
    }
    return client.post(f"/frota/veiculo/{carro.pk}/ipva/", dados)


def test_ipva_pago_antes_da_compra_e_recusado(usuario_logado, carro):
    resposta = _post_ipva(usuario_logado, carro, carro.data_aquisicao - timedelta(days=1))
    assert resposta.status_code == 200
    assert (
        f"O carro foi comprado em {carro.data_aquisicao:%d/%m/%Y} — "
        "IPVA pago antes disso foi do dono anterior."
    ) in resposta.content.decode()
    assert not PagamentoIpva.objects.exists()


def test_ipva_pago_depois_da_venda_e_recusado(usuario_logado, carro):
    venda = HOJE - timedelta(days=30)
    Veiculo.objects.filter(pk=carro.pk).update(
        status=Veiculo.Status.VENDIDO, data_venda=venda, valor_venda=Decimal("35000")
    )
    resposta = _post_ipva(usuario_logado, carro, venda + timedelta(days=1))
    assert resposta.status_code == 200
    assert f"O carro foi vendido em {venda:%d/%m/%Y}" in resposta.content.decode()
    assert not PagamentoIpva.objects.exists()


def test_ipva_pago_enquanto_o_carro_e_da_empresa_e_salvo(usuario_logado, carro):
    resposta = _post_ipva(usuario_logado, carro, carro.data_aquisicao)
    assert resposta.status_code == 302
    assert PagamentoIpva.objects.filter(veiculo=carro).count() == 1


# --- Ficha aberta antes de uma leitura de KM --------------------------------------------


def _dados_da_ficha(carro, **extra):
    dados = {
        k: ("" if v is None else v)
        for k, v in model_to_dict(carro).items()
        if k not in ("foto", "documento")
    }
    dados.update(extra)
    return dados


def test_salvar_ficha_sem_mexer_no_km_mantem_a_leitura_feita_depois(usuario_logado, carro):
    # a tela foi aberta com 30.000; enquanto isso a leitura levou o carro a 30.100
    mostrado = carro.km_atual
    pagina = usuario_logado.get(f"/frota/veiculo/{carro.pk}/editar/").content.decode()
    assert 'name="initial-km_atual"' in pagina
    Veiculo.objects.filter(pk=carro.pk).update(km_atual=30_100)
    dados = _dados_da_ficha(carro, km_atual=str(mostrado), **{"initial-km_atual": str(mostrado)})
    resposta = usuario_logado.post(f"/frota/veiculo/{carro.pk}/editar/", dados)
    assert resposta.status_code == 302, resposta.content.decode()
    carro.refresh_from_db()
    assert carro.km_atual == 30_100


def test_salvar_ficha_antiga_nao_volta_o_km_corrigido_para_o_errado(usuario_logado, carro):
    # tela aberta com 130.000 (leitura errada); a leitura foi corrigida para 30.500
    Veiculo.objects.filter(pk=carro.pk).update(km_atual=130_000)
    carro.refresh_from_db()
    dados = _dados_da_ficha(carro, km_atual="130000", **{"initial-km_atual": "130000"})
    Veiculo.objects.filter(pk=carro.pk).update(km_atual=30_500)
    resposta = usuario_logado.post(f"/frota/veiculo/{carro.pk}/editar/", dados)
    assert resposta.status_code == 302
    carro.refresh_from_db()
    assert carro.km_atual == 30_500


def test_km_mudado_na_ficha_continua_sem_poder_diminuir(usuario_logado, carro):
    dados = _dados_da_ficha(carro, km_atual="29000", **{"initial-km_atual": "30000"})
    resposta = usuario_logado.post(f"/frota/veiculo/{carro.pk}/editar/", dados)
    assert resposta.status_code == 200
    assert "O KM do carro não diminui por aqui" in resposta.content.decode()
    dados = _dados_da_ficha(carro, km_atual="30.200", **{"initial-km_atual": "30000"})
    resposta = usuario_logado.post(f"/frota/veiculo/{carro.pk}/editar/", dados)
    assert resposta.status_code == 302
    carro.refresh_from_db()
    assert carro.km_atual == 30_200


# --- Observações em várias linhas na edição em linha -----------------------------------


def test_fornecedor_editado_na_linha_mantem_as_linhas_das_observacoes(usuario_logado, db):
    Fornecedor.objects.create(nome="Oficina Teste", observacoes="Atende sábado\nFalar com Zé")
    pagina = usuario_logado.get("/frota/fornecedores/").content.decode()
    assert "Atende sábado\nFalar com Zé</textarea>" in pagina
    assert 'name="observacoes" value=' not in pagina


def test_categoria_editada_na_linha_mantem_as_linhas_das_observacoes(usuario_logado, db):
    Categoria.objects.create(
        nome="Hatch", valor_semanal_referencia=Decimal("600"), observacoes="Linha 1\nLinha 2"
    )
    pagina = usuario_logado.get("/frota/categorias/").content.decode()
    assert "Linha 1\nLinha 2</textarea>" in pagina
    assert 'name="observacoes" value=' not in pagina


# --- Cadastro com erro: uma explicação só ----------------------------------------------


def test_categoria_repetida_mostra_o_erro_sem_repetir(usuario_logado, db):
    Categoria.objects.create(nome="Leigo Cat", valor_semanal_referencia=Decimal("600"))
    resposta = usuario_logado.post(
        "/frota/categorias/",
        {"nome": "leigo cat", "valor_semanal_referencia": "600,00", "observacoes": ""},
    )
    pagina = resposta.content.decode()
    assert "Não foi possível cadastrar" not in pagina
    assert "Não foi possível salvar" in pagina  # o resumo do formulário
    assert pagina.count("Já existe a categoria Leigo Cat.") == 2  # resumo + campo


# --- Linha do tempo do carro ------------------------------------------------------------


def test_linha_do_tempo_do_mesmo_dia_na_ordem_certa_e_completa(carro, cliente):
    dia = HOJE - timedelta(days=1)
    substituto = Veiculo.objects.create(placa="LKJ7N89", marca_modelo="Onix", km_atual=10_000)
    oficina = Fornecedor.objects.create(nome="By Car")
    alocacao = Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=dia,
        valor_semanal=Decimal("650.00"),
        km_entrega=30_000,
    )
    Vistoria.objects.create(alocacao=alocacao, tipo=Vistoria.Tipo.ENTRADA, data=dia, km=30_000)
    Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=dia,
        km=30_010,
        descricao="Barulho no freio dianteiro",
        oficina=oficina,
        data_entrada=dia,
        data_saida=dia,
    )
    TrocaTemporaria.objects.create(
        alocacao=alocacao,
        veiculo_substituto=substituto,
        data_retirada=dia,
        km_retirada=10_000,
        data_devolucao=dia,
        km_devolucao=10_050,
    )
    Vistoria.objects.create(alocacao=alocacao, tipo=Vistoria.Tipo.SAIDA, data=dia, km=30_020)
    alocacao.encerrar(data_termino=dia, km_devolucao=30_020)

    eventos = linha_do_tempo(carro)
    tipos = [e["tipo"] for e in eventos]
    # mais recente no topo
    assert tipos[0] == "Devolução"
    assert tipos[-1] == "Alocação"
    assert tipos.index("Vistoria (devolução pelo motorista)") < tipos.index("Fim da troca")
    assert tipos.index("Fim da troca") < tipos.index("Carro na oficina")
    assert tipos.index("Saiu da oficina") < tipos.index("Manutenção")
    assert tipos.index("Manutenção") < tipos.index("Vistoria (entrega ao motorista)")
    textos = {e["tipo"]: e["descricao"] for e in eventos}
    assert "LKJ7N89" in textos["Fim da troca"]
    assert "Barulho no freio dianteiro" in textos["Manutenção"]
    assert "By Car" in textos["Manutenção"]


# --- Revisão: base do aviso de KM, caixa que não some e linha do tempo sem N+1 ----------


def test_aviso_de_km_da_venda_conta_da_ultima_anotacao_de_km(carro):
    # KM anotado há 40 dias; depois o carro só passou pela oficina (sem KM anotado).
    # 19.000 km em 40 dias é normal: a saída da oficina não pode virar a base do aviso
    _leitura(carro, HOJE - timedelta(days=40), 31_000)
    Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=HOJE - timedelta(days=10),
        descricao="Freio",
        data_entrada=HOJE - timedelta(days=10),
        data_saida=HOJE - timedelta(days=2),
    )
    carro.refresh_from_db()
    assert desmobilizacao.suspeita_do_km_da_venda(carro, 50_000, HOJE) is None
    # o absurdo continua avisando, e a mensagem cita a data da leitura de verdade
    aviso = desmobilizacao.suspeita_do_km_da_venda(carro, 310_000, HOJE)
    assert f"em {HOJE - timedelta(days=40):%d/%m/%Y}" in aviso
    # a data da venda continua presa à saída da oficina (lançamento mais recente)
    assert desmobilizacao.ultimo_lancamento_do_carro(carro) == (
        "saída da oficina",
        HOJE - timedelta(days=2),
    )


def test_caixa_confirmo_o_km_continua_quando_outro_campo_da_erro(usuario_logado, carro):
    _leitura(carro, HOJE - timedelta(days=5), 31_000)
    url = f"/frota/veiculo/{carro.pk}/vender/"
    dados = {"data": HOJE.isoformat(), "valor": "", "km": "310.000", "confirmar_km": "1"}
    resposta = usuario_logado.post(url, dados)
    pagina = resposta.content.decode()
    assert resposta.status_code == 200
    # a pessoa já marcou: a caixa volta marcada, sem ela ter de marcar de novo
    assert (
        'name="confirmar_km" value="1" class="mt-1 shrink-0" style="width:auto" checked' in pagina
    )
    carro.refresh_from_db()
    assert carro.status != Veiculo.Status.VENDIDO
    assert carro.km_atual == 31_000


def test_linha_do_tempo_sem_consulta_por_manutencao(carro, django_assert_max_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    oficina = Fornecedor.objects.create(nome="By Car")

    def _manutencao(dias):
        Manutencao.objects.create(
            veiculo=carro,
            tipo=Manutencao.Tipo.CORRETIVA,
            data=HOJE - timedelta(days=dias),
            descricao="Freio",
            oficina=oficina,
            data_entrada=HOJE - timedelta(days=dias),
            data_saida=HOJE - timedelta(days=dias - 1),
        )

    _manutencao(30)
    with CaptureQueriesContext(connection) as uma:
        linha_do_tempo(carro)
    for dias in (20, 15, 10, 5):
        _manutencao(dias)
    with django_assert_max_num_queries(len(uma.captured_queries)):
        eventos = linha_do_tempo(carro)
    assert sum(e["tipo"] == "Saiu da oficina" for e in eventos) == 5
