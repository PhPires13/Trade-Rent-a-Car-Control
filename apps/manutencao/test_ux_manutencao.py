"""Carro na oficina, edição de manutenção e preventivas focadas (propostas #9 e #14)."""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.financeiro.models import Cobranca
from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.pessoas.models import Cliente

HOJE = date.today()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="OFC1A23", marca_modelo="Onix", km_atual=100_000)


@pytest.fixture
def oleo(db):
    return ItemPreventiva.objects.get(nome="Troca de óleo e filtro")  # 10.000 km


@pytest.fixture
def cliente(db):
    return Cliente.objects.create(nome="Bruna", cpf_cnpj="222.333.444-55")


def _na_oficina(veiculo, dias=3, **extra):
    dados = {
        "veiculo": veiculo,
        "tipo": Manutencao.Tipo.CORRETIVA,
        "data": HOJE - timedelta(days=dias),
        "descricao": "Embreagem",
        "data_entrada": HOJE - timedelta(days=dias),
    }
    dados.update(extra)
    return Manutencao.objects.create(**dados)


def _alocar(veiculo, cliente):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=HOJE - timedelta(days=10),
        valor_semanal=Decimal("650.00"),
        km_entrega=veiculo.km_atual,
    )


def _dados_registro(**extra):
    dados = {
        "tipo": "corretiva",
        "data": HOJE.isoformat(),
        "km": "100000",
        "descricao": "Troca da embreagem",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def _json(html, elemento):
    inicio = html.index(f'id="{elemento}"')
    inicio = html.index(">", inicio) + 1
    return json.loads(html[inicio : html.index("</script>", inicio)])


# ---------- #9: carro na oficina ----------


def test_selo_na_oficina_no_hub_e_no_detalhe(usuario_logado, veiculo):
    _na_oficina(veiculo, dias=4)
    hub = usuario_logado.get("/frota/").content.decode()
    assert "Na oficina desde" in hub
    assert "(4 dias)" in hub
    detalhe = usuario_logado.get(f"/frota/veiculo/{veiculo.pk}/").content.decode()
    assert "Na oficina desde" in detalhe


def test_carro_que_saiu_da_oficina_nao_tem_selo(usuario_logado, veiculo):
    _na_oficina(veiculo, data_saida=HOJE)
    assert "Na oficina desde" not in usuario_logado.get("/frota/").content.decode()


def test_quadro_mostra_quem_esta_na_oficina_agora(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo)
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "Na oficina agora (1)" in html
    assert f"/manutencao/voltou/{manutencao.pk}/" in html


def test_voltou_da_oficina_fecha_e_libera_o_carro(usuario_logado, veiculo):
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save()
    manutencao = _na_oficina(veiculo, dias=5)
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "custo_real": "1.250,00", "pagamento_custo": "pago"},
    )
    assert resposta.status_code == 302
    manutencao.refresh_from_db()
    veiculo.refresh_from_db()
    assert manutencao.data_saida == HOJE
    assert manutencao.custo_real == Decimal("1250.00")
    assert manutencao.pagamento_custo == "pago"
    assert manutencao.dias_parado == 5  # parou de contar
    assert veiculo.status == Veiculo.Status.DISPONIVEL


def test_voltou_nao_mexe_no_carro_alocado(usuario_logado, veiculo, cliente):
    _alocar(veiculo, cliente)
    manutencao = _na_oficina(veiculo)
    usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"},
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.ALOCADO


def test_voltou_nao_libera_carro_emprestado_como_substituto(usuario_logado, veiculo, cliente):
    principal = Veiculo.objects.create(placa="PRI1A23", marca_modelo="Gol", km_atual=50_000)
    alocacao = _alocar(principal, cliente)
    TrocaTemporaria.objects.create(
        alocacao=alocacao, veiculo_substituto=veiculo, data_retirada=HOJE, km_retirada=100_000
    )
    Veiculo.objects.filter(pk=veiculo.pk).update(status=Veiculo.Status.EM_MANUTENCAO)
    manutencao = _na_oficina(veiculo)
    usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/",
        {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"},
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_voltou_espera_a_outra_manutencao_em_aberto(usuario_logado, veiculo):
    Veiculo.objects.filter(pk=veiculo.pk).update(status=Veiculo.Status.EM_MANUTENCAO)
    primeira = _na_oficina(veiculo)
    _na_oficina(veiculo, descricao="Funilaria")
    usuario_logado.post(
        f"/manutencao/voltou/{primeira.pk}/",
        {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"},
    )
    veiculo.refresh_from_db()
    assert veiculo.status == Veiculo.Status.EM_MANUTENCAO


def test_voltou_nao_aceita_saida_antes_da_entrada_nem_futura(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo, dias=2)
    for data in (HOJE - timedelta(days=5), HOJE + timedelta(days=1)):
        resposta = usuario_logado.post(
            f"/manutencao/voltou/{manutencao.pk}/",
            {"data_saida": data.isoformat(), "pagamento_custo": "pendente"},
        )
        assert resposta.status_code == 200
    manutencao.refresh_from_db()
    assert manutencao.data_saida is None


def test_voltou_respeita_o_endereco_de_volta_do_site(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo)
    dados = {"data_saida": HOJE.isoformat(), "pagamento_custo": "pendente"}
    resposta = usuario_logado.post(
        f"/manutencao/voltou/{manutencao.pk}/", dict(dados, volta="https://golpe.example/")
    )
    assert resposta["Location"] == f"/manutencao/historico/{veiculo.pk}/"


def test_editar_manutencao_registrada(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo)
    assert usuario_logado.get(f"/manutencao/editar/{manutencao.pk}/").status_code == 200
    resposta = usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/",
        _dados_registro(
            descricao="Embreagem completa",
            data=manutencao.data.isoformat(),
            data_entrada=manutencao.data_entrada.isoformat(),
            custo_real="980,00",
        ),
    )
    assert resposta.status_code == 302
    manutencao.refresh_from_db()
    assert manutencao.descricao == "Embreagem completa"
    assert manutencao.custo_real == Decimal("980.00")
    assert manutencao.data_saida is None


def test_historico_tem_editar_pago_e_voltou(usuario_logado, veiculo):
    # "Falta pagar" só aparece quando há custo a pagar (sem custo, mostra "—")
    aberta = _na_oficina(veiculo, custo_real=Decimal("800.00"))
    html = usuario_logado.get(f"/manutencao/historico/{veiculo.pk}/").content.decode()
    assert f"/manutencao/editar/{aberta.pk}/" in html
    assert f"/manutencao/voltou/{aberta.pk}/" in html
    assert "Falta pagar" in html
    assert "ainda na oficina" in html


def test_nova_alocacao_avisa_carro_na_oficina(usuario_logado, veiculo, cliente):
    _na_oficina(veiculo, dias=2)
    html = usuario_logado.get("/alocacoes/nova/").content.decode()
    assert str(veiculo.pk) in _json(html, "carros-na-oficina")
    resposta = usuario_logado.post(
        "/alocacoes/nova/",
        {
            "veiculo": veiculo.pk,
            "cliente": cliente.pk,
            "data_inicio": HOJE.isoformat(),
            "valor_semanal": "650,00",
            "km_entrega": "100000",
            "limite_km": "ilimitado",
        },
        follow=True,
    )
    assert "consta como na oficina" in resposta.content.decode()


# ---------- #14: formulário de manutenção ----------


def test_registrar_ja_vem_com_data_km_e_item(usuario_logado, veiculo, oleo):
    form = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/?item={oleo.pk}").context["form"]
    assert form.initial["data"] == HOJE
    assert form.initial["km"] == 100_000
    assert form.initial["item"] == oleo.pk
    assert "tipo" not in form.fields  # o tipo sai do item (preventiva), não se escolhe


def test_registrar_vinculado_ao_sinistro(usuario_logado, veiculo):
    from apps.sinistros.models import Sinistro

    campos = {f.name for f in Sinistro._meta.fields}
    dados = {"veiculo": veiculo, "data": HOJE}
    if "descricao" in campos:
        dados["descricao"] = "Batida"
    sinistro = Sinistro.objects.create(**dados)
    form = usuario_logado.get(
        f"/manutencao/registrar/{veiculo.pk}/?sinistro={sinistro.pk}"
    ).context["form"]
    assert form.initial["sinistro"] == sinistro.pk
    assert "item" not in form.initial  # sem item: grava como corretiva
    assert form.initial["data_entrada"] == HOJE


def test_item_do_plano_exige_km(usuario_logado, veiculo, oleo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(item=oleo.pk, tipo="preventiva", km=""),
    )
    assert resposta.status_code == 200
    assert "próxima troca recomeçar" in resposta.content.decode()
    assert not veiculo.manutencoes.exists()


def test_saida_antes_da_entrada_e_recusada(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(data_entrada="2026-07-10", data_saida="2026-07-05"),
    )
    assert resposta.status_code == 200
    assert "não pode ser antes da entrada" in resposta.content.decode()


def test_valor_do_cliente_so_vale_quando_o_cliente_paga(usuario_logado, veiculo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(responsavel="empresa", valor_cobrado_cliente="300,00"),
    )
    assert veiculo.manutencoes.get().valor_cobrado_cliente is None


def test_ja_gerar_a_cobranca_ao_registrar(usuario_logado, veiculo, cliente):
    _alocar(veiculo, cliente)
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(responsavel="cliente", valor_cobrado_cliente="300,00", gerar_cobranca="on"),
    )
    assert resposta.status_code == 302
    manutencao = veiculo.manutencoes.get()
    assert manutencao.cobranca_repasse is not None
    assert manutencao.cobranca_repasse.valor == Decimal("300.00")
    assert Cobranca.objects.filter(origem=Cobranca.Origem.REPASSE_MANUTENCAO).count() == 1


def test_registrar_volta_para_onde_a_pessoa_veio(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        _dados_registro(volta="/manutencao/preventivas/#veiculo-1"),
    )
    assert resposta["Location"] == "/manutencao/preventivas/#veiculo-1"


# ---------- #14: preventivas focadas no que precisa de atenção ----------


def _preventiva(veiculo, item, km):
    return Manutencao.objects.create(
        veiculo=veiculo, item=item, tipo="preventiva", data=HOJE, km=km, descricao="Troca"
    )


def test_preventivas_abre_so_no_que_precisa_de_atencao(usuario_logado, oleo):
    vencido = Veiculo.objects.create(placa="VEN1A23", marca_modelo="Gol", km_atual=100_000)
    _preventiva(vencido, oleo, 88_000)  # passou 2.000 km
    em_dia = Veiculo.objects.create(placa="DIA1A23", marca_modelo="Gol", km_atual=100_000)
    _preventiva(em_dia, oleo, 99_000)

    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "VEN1A23" in html
    assert "vencida há 2.000 km" in html
    # o seletor "Qual carro?" lista todos; o quadro só o que precisa de atenção
    assert f'id="veiculo-{em_dia.pk}"' not in html
    assert "?ver=todas" in html
    assert f"?item={oleo.pk}" in html  # botão "Registrar feita"

    todas = usuario_logado.get("/manutencao/preventivas/?ver=todas").content.decode()
    assert "DIA1A23" in todas
    assert f'id="veiculo-{em_dia.pk}"' in todas


def test_preventivas_ordena_o_mais_atrasado_primeiro(usuario_logado, oleo):
    pouco = Veiculo.objects.create(placa="AAA1A11", marca_modelo="Gol", km_atual=100_000)
    _preventiva(pouco, oleo, 90_500)  # faltam 500
    muito = Veiculo.objects.create(placa="ZZZ9Z99", marca_modelo="Gol", km_atual=100_000)
    _preventiva(muito, oleo, 80_000)  # vencida há 10.000
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert html.index(f'id="veiculo-{muito.pk}"') < html.index(f'id="veiculo-{pouco.pk}"')


def test_preventivas_sem_alerta_mostra_tudo_em_dia(usuario_logado, veiculo):
    html = usuario_logado.get("/manutencao/preventivas/").content.decode()
    assert "Nenhuma preventiva vencida ou perto de vencer" in html
    assert "nunca registrado" in html  # o carro nunca teve as trocas informadas


def test_hub_leva_direto_ao_carro_nas_preventivas(usuario_logado, oleo):
    vencido = Veiculo.objects.create(placa="VEN1A23", marca_modelo="Gol", km_atual=100_000)
    _preventiva(vencido, oleo, 88_000)
    html = usuario_logado.get("/frota/").content.decode()
    assert f"/manutencao/preventivas/#veiculo-{vencido.pk}" in html
