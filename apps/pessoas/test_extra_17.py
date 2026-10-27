"""Etapa 17 (achados extras): ação principal da ficha de cliente recém-cadastrado."""

from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.pessoas.models import Cliente


def _topo(html):
    # cabeçalho da ficha: até o cartão de próximos passos/dados cadastrais
    return html.split("Dados cadastrais")[0]


def test_cliente_novo_ve_alocar_e_nao_recebi(usuario_logado, db):
    cliente = Cliente.objects.create(nome="Novo", cpf_cnpj="111.222.333-44")
    html = usuario_logado.get(reverse("pessoas:detalhe", args=[cliente.pk])).content.decode()
    topo = _topo(html)
    assert "✓ Recebi" not in topo
    assert "+ Alocar um carro" in topo


def test_cliente_com_carro_ve_recebi_no_topo(usuario_logado, db):
    cliente = Cliente.objects.create(nome="Ativo", cpf_cnpj="111.222.333-44")
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol", km_atual=50_000)
    Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=timezone.localdate(),
        valor_semanal=Decimal("650.00"),
        dia_vencimento=timezone.localdate().weekday(),
        km_entrega=50_000,
    )
    html = usuario_logado.get(reverse("pessoas:detalhe", args=[cliente.pk])).content.decode()
    assert "✓ Recebi" in _topo(html)
