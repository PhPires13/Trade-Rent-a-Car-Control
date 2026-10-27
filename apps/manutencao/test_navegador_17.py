"""Etapa 17 (navegador): oficina do carro fora de locação, item do plano e placa primeiro."""

import pytest
from django.utils import timezone

from apps.frota.models import Veiculo
from apps.manutencao.models import ItemPreventiva, Manutencao
from apps.manutencao.views import IntervaloPersonalizadoForm

HOJE = timezone.localdate()


@pytest.fixture
def veiculo(db):
    return Veiculo.objects.create(placa="NAV2M17", marca_modelo="Onix", km_atual=50_000)


@pytest.fixture
def oleo(db):
    return ItemPreventiva.objects.get(nome="Troca de óleo e filtro")


def _dados(**extra):
    dados = {
        "data": HOJE.isoformat(),
        "km": "50000",
        "descricao": "Serviço",
        "origem_custo": "particular",
        "responsavel": "empresa",
        "pagamento_custo": "pendente",
    }
    dados.update(extra)
    return dados


def _na_oficina(veiculo):
    veiculo.status = Veiculo.Status.EM_MANUTENCAO
    veiculo.save(update_fields=["status"])
    return Manutencao.objects.create(
        veiculo=veiculo, tipo="corretiva", data=HOJE, descricao="Freio", data_entrada=HOJE
    )


def test_fora_de_locacao_sai_da_oficina_sem_dizer_que_vai_ser_alugado(usuario_logado, veiculo):
    veiculo.uso = Veiculo.Uso.FORA_LOCACAO
    veiculo.save(update_fields=["uso"])
    manutencao = _na_oficina(veiculo)
    tela = usuario_logado.get(f"/manutencao/voltou/{manutencao.pk}/").content.decode()
    assert "fora de locação" in tela
    assert "disponível</strong> para alugar" not in tela
    html = usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/", follow=True).content.decode()
    assert "saiu da oficina (fica fora de locação)" in html
    assert "disponível para alugar" not in html


def test_carro_de_locacao_continua_com_o_aviso_de_disponivel(usuario_logado, veiculo):
    manutencao = _na_oficina(veiculo)
    html = usuario_logado.post(f"/manutencao/apagar/{manutencao.pk}/", follow=True).content.decode()
    assert "voltou a ficar disponível para alugar" in html


def test_item_do_plano_sem_descricao_usa_o_nome_do_item(usuario_logado, veiculo, oleo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(item=oleo.pk, descricao="", token="t1")
    )
    manutencao = veiculo.manutencoes.get()
    assert manutencao.descricao == oleo.nome


def test_sem_item_e_sem_descricao_erro_em_portugues(usuario_logado, veiculo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(descricao="", token="t2")
    )
    assert "Descreva o serviço feito" in resposta.content.decode()
    assert not veiculo.manutencoes.exists()


def test_descricao_e_km_sem_required_do_navegador(usuario_logado, veiculo):
    html = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").content.decode()
    inicio = html.index('name="descricao"')
    assert "required" not in html[html.rindex("<textarea", 0, inicio) : html.index(">", inicio)]
    # o "(opcional)" do KM some quando o item tem intervalo (Alpine)
    assert "x-show=\"tipos[item] !== 'preventiva'\"" in html


def test_km_aceita_ponto_de_milhar(usuario_logado, veiculo, oleo):
    usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(item=oleo.pk, km="50.000", token="t3")
    )
    assert veiculo.manutencoes.get().km == 50_000


def test_km_obrigatorio_com_item_de_intervalo(usuario_logado, veiculo, oleo):
    resposta = usuario_logado.post(
        f"/manutencao/registrar/{veiculo.pk}/", _dados(item=oleo.pk, km="", token="t4")
    )
    assert "Informe o KM" in resposta.content.decode()


def test_plano_mostra_placa_primeiro(veiculo):
    form = IntervaloPersonalizadoForm()
    rotulos = [rotulo for _valor, rotulo in form.fields["veiculo"].choices]
    assert "NAV2M17 — Onix" in rotulos


def test_campo_km_do_mesmo_tamanho_dos_outros(usuario_logado, veiculo):
    """CampoKm sem a classe grande (text-lg) da leitura mensal — a linha Data/KM fica igual."""
    html = usuario_logado.get(f"/manutencao/registrar/{veiculo.pk}/").content.decode()
    inicio = html.rindex("<input", 0, html.index('name="km"'))
    campo = html[inicio : html.index(">", inicio)]
    assert "text-lg" not in campo
    assert 'inputmode="numeric"' in campo


def test_editar_com_item_e_descricao_apagada_usa_o_nome_do_item(usuario_logado, veiculo, oleo):
    manutencao = Manutencao.objects.create(
        veiculo=veiculo, tipo="preventiva", item=oleo, data=HOJE, km=50_000, descricao="Óleo"
    )
    usuario_logado.post(
        f"/manutencao/editar/{manutencao.pk}/", _dados(item=oleo.pk, descricao="  ", token="t5")
    )
    manutencao.refresh_from_db()
    assert manutencao.descricao == oleo.nome
