"""Etapa 18b — achados dos testes no navegador na área de manutenção.

Intervalo do plano "15.000" virava 15 km, KM de manutenção com data passada
sem conferência com as leituras em volta, item fora de uso apagado ao editar,
conserto de sinistro com proteção contado como despesa e o carro alugado que
vai para a oficina sem sugerir o substituto.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.manutencao.models import IntervaloPersonalizado, ItemPreventiva, Manutencao
from apps.manutencao.views import suspeita_do_km_da_manutencao
from apps.pessoas.models import Cliente
from apps.sinistros.models import Sinistro


@pytest.fixture
def carro(db):
    return Veiculo.objects.create(placa="QXA1B23", marca_modelo="Onix", km_atual=38_200)


def _leitura(veiculo, data, km):
    registro = RegistroKm(
        veiculo=veiculo, mes_referencia=data.replace(day=1), data_leitura=data, km=km
    )
    registro.full_clean()
    registro.save()
    return registro


def _mensagens(resposta):
    return [str(m) for m in resposta.context["messages"]] if resposta.context else []


# --- intervalo do plano com ponto de milhar ------------------------------------


def test_intervalo_personalizado_com_ponto_grava_o_numero_inteiro(usuario_logado, carro):
    alinhamento = ItemPreventiva.objects.get(nome="Alinhamento")
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "novo_intervalo",
            "veiculo": carro.pk,
            "item": alinhamento.pk,
            "intervalo_km": "20.000",
        },
        follow=True,
    )
    assert IntervaloPersonalizado.objects.get(veiculo=carro, item=alinhamento).intervalo_km == (
        20_000
    )
    texto = " ".join(_mensagens(resposta))
    assert f"Alinhamento do {carro.placa} agora é a cada 20.000 km" in texto
    assert "padrão do plano:" in texto


def test_item_do_plano_com_ponto_grava_15000_e_mostra_na_mensagem(usuario_logado, db):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "editar_item",
            "item_id": item.pk,
            "nome": item.nome,
            "intervalo_km_padrao": "15.000",
            "ativo": "on",
        },
        follow=True,
    )
    item.refresh_from_db()
    assert item.intervalo_km_padrao == 15_000
    assert "a cada 15.000 km" in " ".join(_mensagens(resposta))


def test_novo_item_com_ponto_grava_o_numero_inteiro(usuario_logado, db):
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_item", "nome": "Filtro de cabine", "intervalo_km_padrao": "15.000 km"},
        follow=True,
    )
    assert ItemPreventiva.objects.get(nome="Filtro de cabine").intervalo_km_padrao == 15_000
    assert "a cada 15.000 km" in " ".join(_mensagens(resposta))


def test_intervalo_pequeno_demais_e_recusado_com_sugestao(usuario_logado, carro):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_intervalo", "veiculo": carro.pk, "item": item.pk, "intervalo_km": "15"},
    )
    assert resposta.status_code == 200
    erro = resposta.context["form_intervalo"].errors["intervalo_km"][0]
    assert "curto demais" in erro
    assert "15.000" in erro
    assert not IntervaloPersonalizado.objects.exists()


def test_editar_item_com_intervalo_pequeno_nao_grava(usuario_logado, db):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    antes = item.intervalo_km_padrao
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {
            "acao": "editar_item",
            "item_id": item.pk,
            "nome": item.nome,
            "intervalo_km_padrao": "10",
            "ativo": "on",
        },
        follow=True,
    )
    item.refresh_from_db()
    assert item.intervalo_km_padrao == antes
    assert any("curto demais" in m for m in _mensagens(resposta))


def test_intervalo_duplicado_tem_texto_simples(usuario_logado, carro):
    item = ItemPreventiva.objects.get(nome="Alinhamento")
    IntervaloPersonalizado.objects.create(veiculo=carro, item=item, intervalo_km=20_000)
    resposta = usuario_logado.post(
        "/manutencao/plano/",
        {"acao": "novo_intervalo", "veiculo": carro.pk, "item": item.pk, "intervalo_km": "25000"},
    )
    html = resposta.content.decode()
    assert f"O {carro.placa} já tem um intervalo próprio para Alinhamento" in html
    assert "já existe" not in html  # texto padrão do Django
    assert IntervaloPersonalizado.objects.get(veiculo=carro, item=item).intervalo_km == 20_000


def test_tabela_do_plano_usa_campo_de_texto_com_milhar(usuario_logado, db):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    html = usuario_logado.get("/manutencao/plano/").content.decode()
    assert f'id="intervalo-{item.pk}" name="intervalo_km_padrao"' in html
    assert '<input type="number"' not in html
    assert 'value="10.000"' in html


def test_str_do_intervalo_tem_milhar(carro):
    item = ItemPreventiva.objects.get(nome="Alinhamento")
    intervalo = IntervaloPersonalizado.objects.create(veiculo=carro, item=item, intervalo_km=20_000)
    assert str(intervalo).endswith("20.000 km")


# --- KM de manutenção com data passada -----------------------------------------


@pytest.fixture
def carro_com_leituras(carro):
    carro.km_compra = 38_200
    carro.data_aquisicao = date(2026, 3, 1)
    carro.save()
    _leitura(carro, date(2026, 8, 31), 85_670)
    _leitura(carro, date(2026, 9, 30), 91_370)
    _leitura(carro, date(2026, 10, 31), 97_260)
    carro.refresh_from_db()
    return carro


def test_km_maior_que_a_leitura_seguinte_pede_confirmacao(carro_com_leituras):
    aviso = suspeita_do_km_da_manutencao(carro_com_leituras, 97_900, date(2026, 9, 15))
    assert aviso is not None
    assert "em 31/08/2026 o carro marcava 85.670 km" in aviso
    assert "em 30/09/2026 marcava 91.370 km" in aviso


def test_km_com_digito_faltando_na_data_passada_pede_confirmacao(carro_com_leituras):
    aviso = suspeita_do_km_da_manutencao(carro_com_leituras, 9_100, date(2026, 9, 14))
    assert aviso is not None
    assert "85.670 km" in aviso


def test_km_absurdo_na_data_passada_nao_fala_em_mesmo_dia(carro_com_leituras):
    aviso = suspeita_do_km_da_manutencao(carro_com_leituras, 970_000, date(2026, 9, 15))
    assert aviso is not None
    assert "mesmo dia" not in aviso
    assert "por dia" not in aviso


def test_km_que_cabe_entre_as_leituras_passa(carro_com_leituras):
    assert suspeita_do_km_da_manutencao(carro_com_leituras, 88_000, date(2026, 9, 15)) is None


def test_ultima_troca_antes_da_compra_nao_passa_do_km_da_compra(carro_com_leituras):
    aviso = suspeita_do_km_da_manutencao(carro_com_leituras, 40_000, date(2026, 1, 10))
    assert aviso is not None
    assert "38.200 km (compra)" in aviso
    assert suspeita_do_km_da_manutencao(carro_com_leituras, 30_000, date(2026, 1, 10)) is None


def test_informar_ultima_troca_com_km_impossivel_nao_grava_sem_confirmar(
    usuario_logado, carro_com_leituras
):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    dados = {
        "item": item.pk,
        "data": "2026-09-15",
        "km": "97.900",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
        "token": "t1",
    }
    url = f"/manutencao/registrar/{carro_com_leituras.pk}/?item={item.pk}&ultima=1"
    resposta = usuario_logado.post(url, dados)
    assert resposta.status_code == 200
    assert resposta.context["form"].km_suspeito
    assert not Manutencao.objects.exists()
    carro_com_leituras.refresh_from_db()
    assert carro_com_leituras.km_atual == 97_260

    usuario_logado.post(url, {**dados, "km": "88.000", "token": "t2"})
    assert Manutencao.objects.get().km == 88_000


# --- item fora de uso não some na edição ---------------------------------------


def test_editar_sem_mudar_mantem_o_item_fora_de_uso_e_o_tipo(usuario_logado, carro):
    item = ItemPreventiva.objects.get(nome="Troca de óleo e filtro")
    manutencao = Manutencao.objects.create(
        veiculo=carro,
        item=item,
        tipo=Manutencao.Tipo.PREVENTIVA,
        data=date(2026, 10, 1),
        km=38_200,
        descricao="Troca de óleo e filtro",
    )
    item.ativo = False
    item.save()

    tela = usuario_logado.get(f"/manutencao/editar/{manutencao.pk}/")
    html = tela.content.decode()
    assert "Troca de óleo e filtro (fora de uso)" in html
    assert str(item.pk) in tela.context["form"].tipos_por_item()

    usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/",
        {
            "item": item.pk,
            "data": "2026-10-01",
            "km": "38200",
            "descricao": "Troca de óleo e filtro",
            "origem_custo": "particular",
            "responsavel": "empresa",
            "pagamento_custo": "pendente",
        },
    )
    manutencao.refresh_from_db()
    assert manutencao.item_id == item.pk
    assert manutencao.tipo == Manutencao.Tipo.PREVENTIVA


def test_registrar_nova_nao_oferece_item_fora_de_uso(usuario_logado, carro):
    item = ItemPreventiva.objects.get(nome="Alinhamento")
    item.ativo = False
    item.save()
    tela = usuario_logado.get(f"/manutencao/registrar/{carro.pk}/")
    assert item not in tela.context["form"].fields["item"].queryset


# --- sinistro com proteção acionada --------------------------------------------


def test_ida_a_oficina_de_sinistro_com_protecao_vem_como_evento_da_protecao(usuario_logado, carro):
    sinistro = Sinistro.objects.create(
        veiculo=carro,
        data=date(2026, 10, 20),
        envolvido="cliente",
        acionou_protecao=True,
        franquia_valor=Decimal("1500.00"),
    )
    tela = usuario_logado.get(f"/manutencao/registrar/{carro.pk}/?sinistro={sinistro.pk}")
    form = tela.context["form"]
    assert form["origem_custo"].value() == Manutencao.OrigemCusto.PROTECAO
    assert "não conte o valor pago pela Auto Truck" in tela.content.decode()


def test_ida_a_oficina_de_sinistro_sem_protecao_continua_particular(usuario_logado, carro):
    sinistro = Sinistro.objects.create(veiculo=carro, data=date(2026, 10, 20), envolvido="cliente")
    tela = usuario_logado.get(f"/manutencao/registrar/{carro.pk}/?sinistro={sinistro.pk}")
    assert tela.context["form"]["origem_custo"].value() == Manutencao.OrigemCusto.PARTICULAR


# --- carro alugado na oficina: sugere o substituto -----------------------------


def _registrar_oficina(cliente_http, veiculo, token="o1"):
    return cliente_http.post(
        f"/manutencao/registrar/{veiculo.pk}/",
        {
            "data": "2026-11-10",
            "km": "38200",
            "descricao": "Freio",
            "data_entrada": "2026-11-10",
            "origem_custo": "particular",
            "responsavel": "empresa",
            "pagamento_custo": "pendente",
            "token": token,
        },
        follow=True,
    )


def test_carro_alugado_na_oficina_sugere_dar_substituto(usuario_logado, carro):
    cliente = Cliente.objects.create(nome="Eduarda Rocha", cpf_cnpj="52998224725")
    alocacao = Alocacao.objects.create(
        veiculo=carro,
        cliente=cliente,
        data_inicio=date(2026, 10, 1),
        km_entrega=38_000,
        valor_semanal=Decimal("650.00"),
    )
    resposta = _registrar_oficina(usuario_logado, carro)
    html = resposta.content.decode()
    assert "dê um carro substituto para Eduarda Rocha" in html
    assert f"/alocacoes/{alocacao.pk}/troca/?voltar=" in html


def test_carro_livre_na_oficina_nao_sugere_substituto(usuario_logado, carro):
    html = _registrar_oficina(usuario_logado, carro).content.decode()
    assert "carro substituto" not in html
