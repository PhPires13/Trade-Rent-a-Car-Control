"""Etapa 18b — revisão das correções do navegador na área de manutenção.

Dica do intervalo do plano que sumiu com o campo novo, KM de data passada
conferido sem comparar a manutenção com ela mesma, sugestão do substituto só
quando a ida à oficina começa (e nunca com troca já aberta) e o conserto pela
proteção que não entra como despesa.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao, TrocaTemporaria
from apps.frota.desmobilizacao import manutencoes_que_sao_despesa
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.views import suspeita_do_km_da_manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(placa="RVM1B23", marca_modelo="Onix", km_atual=38_200)


def _leitura(veiculo, data, km):
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )
    registro.full_clean()
    registro.save()
    return registro


def _textos(resposta):
    return " ".join(str(m) for m in resposta.context["messages"])


# --- dica do intervalo do plano --------------------------------------------------


def test_novo_item_do_plano_mostra_a_dica_de_vazio_sem_prazo(usuario_logado, db):
    html = usuario_logado.get("/manutencao/plano/").content.decode()
    assert "Vazio = sem prazo fixo" in html


def test_novo_item_sem_intervalo_vira_esporadico(usuario_logado, db):
    usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_item", "nome": "Embreagem", "intervalo_km_padrao": "", "ativo": "on"},
    )
    assert ItemPreventiva.objects.get(nome="Embreagem").intervalo_km_padrao is None


@pytest.mark.parametrize("texto", ["0", "499"])
def test_intervalo_zero_ou_abaixo_do_minimo_e_recusado(usuario_logado, db, texto):
    usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_item", "nome": "Teste", "intervalo_km_padrao": texto, "ativo": "on"},
    )
    assert not ItemPreventiva.objects.filter(nome="Teste").exists()


def test_intervalo_de_500_km_passa(usuario_logado, db):
    usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_item", "nome": "Teste", "intervalo_km_padrao": "500", "ativo": "on"},
    )
    assert ItemPreventiva.objects.get(nome="Teste").intervalo_km_padrao == 500


# --- KM de data passada: a própria manutenção não conta ----------------------------


@pytest.fixture
def com_leituras(carro):
    _leitura(carro, date(2026, 8, 31), 85_670)
    _leitura(carro, date(2026, 10, 31), 97_260)
    carro.refresh_from_db()
    return carro


def test_editar_km_da_manutencao_passada_nao_compara_com_ela_mesma(com_leituras):
    manutencao = Manutencao.objects.create(
        veiculo=com_leituras,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 9, 15),
        km=88_000,
        descricao="Freio",
    )
    # 89.000 cabe entre 85.670 e 97.260; o 88.000 gravado (ela mesma) não é limite
    assert suspeita_do_km_da_manutencao(com_leituras, 89_000, manutencao.data, manutencao) is None
    assert suspeita_do_km_da_manutencao(com_leituras, 87_000, manutencao.data, manutencao) is None


def test_editar_km_da_manutencao_passada_para_fora_avisa(com_leituras):
    manutencao = Manutencao.objects.create(
        veiculo=com_leituras,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 9, 15),
        km=88_000,
        descricao="Freio",
    )
    aviso = suspeita_do_km_da_manutencao(com_leituras, 8_800, manutencao.data, manutencao)
    assert aviso is not None
    assert "85.670 km" in aviso


def test_km_igual_ao_da_leitura_do_mesmo_dia_passa(com_leituras):
    assert suspeita_do_km_da_manutencao(com_leituras, 85_670, date(2026, 8, 31)) is None


def test_data_passada_so_com_anotacao_depois_avisa_sem_falar_entre_os_dois(carro):
    _leitura(carro, date(2026, 10, 31), 50_000)
    carro.refresh_from_db()
    aviso = suspeita_do_km_da_manutencao(carro, 60_000, date(2026, 9, 1))
    assert aviso is not None
    assert "o odômetro não volta" in aviso
    assert "entre os dois" not in aviso


# --- carro substituto ---------------------------------------------------------------


@pytest.fixture
def alugado(carro):
    cliente = Cliente.objects.create(nome="Eduarda Rocha", cpf_cnpj="52998224725")
    return Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=date(2026, 10, 1),
        km_entrega=38_000,
        valor_semanal=Decimal("650.00"),
    )


def _dados(**extra):
    return {
        "data": "2026-11-01",
        "km": "38200",
        "descricao": "Freio",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
        **extra,
    }


def test_carro_com_substituto_ja_aberto_nao_sugere_de_novo(usuario_logado, carro, alugado):
    reserva = Veiculo.objects.create(placa="RES1B23", marca_modelo="Gol", km_atual=10_000)
    TrocaTemporaria.objects.create(
        alocacao=alugado,
        veiculo_substituto=reserva,
        data_retirada=date(2026, 11, 1),
        km_retirada=10_000,
    )
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{carro.pk}/",
        _dados(data_entrada="2026-11-01", token="s1"),
        follow=True,
    )
    assert Manutencao.objects.filter(veiculo=carro).exists()
    assert "carro substituto" not in _textos(resposta)


def test_editar_ida_ja_aberta_nao_repete_a_sugestao(usuario_logado, carro, alugado):
    manutencao = Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 11, 1),
        km=38_200,
        descricao="Freio",
        data_entrada=date(2026, 11, 1),
    )
    resposta = usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/",
        _dados(data_entrada="2026-11-01", descricao="Freio e pastilha"),
        follow=True,
    )
    manutencao.refresh_from_db()
    assert manutencao.descricao == "Freio e pastilha"
    assert "carro substituto" not in _textos(resposta)


def test_editar_abrindo_a_ida_a_oficina_sugere_o_substituto(usuario_logado, carro, alugado):
    manutencao = Manutencao.objects.create(
        veiculo=carro,
        tipo=Manutencao.Tipo.CORRETIVA,
        data=date(2026, 11, 1),
        km=38_200,
        descricao="Freio",
    )
    resposta = usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/",
        _dados(data_entrada="2026-11-01"),
        follow=True,
    )
    assert "dê um carro substituto para Eduarda Rocha" in resposta.content.decode()


def test_nome_do_cliente_na_sugestao_e_escapado(usuario_logado, carro):
    cliente = Cliente.objects.create(nome="<b>Ana</b>", cpf_cnpj="11144477735")
    Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=date(2026, 10, 1),
        km_entrega=38_000,
        valor_semanal=Decimal("650.00"),
    )
    html = usuario_logado.post(
        f"/manutencao/registrar/{carro.pk}/",
        _dados(data_entrada="2026-11-01", token="s2"),
        follow=True,
    ).content.decode()
    assert "&lt;b&gt;Ana&lt;/b&gt;" in html
    assert "<b>Ana</b>" not in html


# --- conserto pela proteção não vira despesa duas vezes ------------------------------


def test_conserto_do_sinistro_com_protecao_gravado_nao_e_despesa(usuario_logado, carro):
    sinistro = Sinistro.objects.create(
        veiculo=carro,
        data=date(2026, 10, 20),
        envolvido="cliente",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
    )
    url = f"/manutencao/registrar/{carro.pk}/?sinistro={sinistro.pk}"
    tela = usuario_logado.get(url)
    dados = {
        nome: valor
        for nome, valor in ((campo.html_name, campo.value()) for campo in tela.context["form"])
        if valor not in (None, "")
    }
    dados.update(
        data="2026-10-21",
        km="38200",
        descricao="Para-choque",
        custo_real="4000,00",
        token=tela.context.get("token", "p1"),
    )
    usuario_logado.post(url, dados)
    manutencao = Manutencao.objects.get(veiculo=carro)
    assert manutencao.origem_custo == Manutencao.OrigemCusto.PROTECAO
    assert manutencao.sinistro_id == sinistro.pk
    assert manutencao not in manutencoes_que_sao_despesa()
