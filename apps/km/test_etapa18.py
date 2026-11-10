"""Etapa 18 — KM: "KM rodado" da 1ª leitura na lista do mês, calculado em lote."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.alocacoes.models import Alocacao
from apps.frota.models import Veiculo
from apps.km.models import RegistroKm
from apps.pessoas.models import Cliente

HOJE = timezone.localdate()


def _leitura(veiculo, data_leitura, km):
    registro = RegistroKm(
        veiculo=veiculo,
        mes_referencia=data_leitura.replace(day=1),
        data_leitura=data_leitura,
        km=km,
    )
    registro.full_clean()
    registro.save()
    return registro


def _alocar(veiculo, cliente, inicio, km_entrega):
    return Alocacao.objects.create(
        veiculo=veiculo,
        cliente=cliente,
        data_inicio=inicio,
        km_entrega=km_entrega,
        valor_semanal=Decimal("650.00"),
    )


def test_lista_mostra_km_rodado_desde_a_entrega_na_primeira_leitura(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="ENT1A18", marca_modelo="Gol", km_atual=30_000)
    cliente = Cliente.objects.create(nome="Lucas", cpf_cnpj="52998224725")
    inicio = HOJE - timedelta(days=20)
    _alocar(veiculo, cliente, inicio, 35_000)
    _leitura(veiculo, HOJE, 37_000)

    html = usuario_logado.get(f"/km/?mes={HOJE:%Y-%m}").content.decode()

    assert f"2.000 km desde a entrega (35.000 em {inicio:%d/%m})" in html
    assert "Lucas" in html  # o cliente da linha continua aparecendo


def test_lista_sem_entrega_continua_com_traco(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="ENT2A18", marca_modelo="Gol", km_atual=30_000)
    _leitura(veiculo, HOJE, 37_000)

    html = usuario_logado.get(f"/km/?mes={HOJE:%Y-%m}").content.decode()

    assert "desde a entrega" not in html


def test_lista_nao_faz_consulta_por_linha(usuario_logado, db):
    """O texto "desde a entrega" sai em lote: 3 carros custam o mesmo que 1."""

    def carro(n):
        veiculo = Veiculo.objects.create(placa=f"LOT{n}A18", marca_modelo="Gol", km_atual=1_000)
        cliente = Cliente.objects.create(nome=f"Cliente {n}", cpf_cnpj=f"000.000.000-0{n}")
        _alocar(veiculo, cliente, HOJE - timedelta(days=10), 1_000)
        _leitura(veiculo, HOJE, 2_000)

    def consultas():
        with CaptureQueriesContext(connection) as ctx:
            html = usuario_logado.get(f"/km/?mes={HOJE:%Y-%m}").content.decode()
        return len(ctx.captured_queries), html.count("desde a entrega")

    usuario_logado.get("/km/")  # aquece a sessão
    carro(1)
    com_um = consultas()
    carro(2)
    carro(3)
    com_tres = consultas()
    assert com_um[1] == 1 and com_tres[1] == 3
    assert com_tres[0] == com_um[0]


# --- revisão adversarial -------------------------------------------------------


def test_entrega_de_outro_ano_mostra_o_ano(usuario_logado, db):
    """Contrato antigo (carga de dados): "em 20/07" sem o ano enganaria."""
    veiculo = Veiculo.objects.create(placa="ANO1A18", marca_modelo="Gol", km_atual=30_000)
    cliente = Cliente.objects.create(nome="Rafael", cpf_cnpj="11144477735")
    inicio = HOJE - timedelta(days=400)
    _alocar(veiculo, cliente, inicio, 35_000)
    _leitura(veiculo, HOJE, 60_000)

    html = usuario_logado.get(f"/km/?mes={HOJE:%Y-%m}").content.decode()

    assert f"25.000 km desde a entrega (35.000 em {inicio:%d/%m/%Y})" in html


def test_erro_no_excedente_nao_vira_tela_de_erro(usuario_logado, db, monkeypatch):
    """A leitura já está gravada: falha no excedente vira aviso, não erro 500."""

    def quebra(registro, hoje=None):
        raise ValueError("contrato com dado ruim")

    monkeypatch.setattr("apps.km.views.gerar_cobrancas_excedente", quebra)
    veiculo = Veiculo.objects.create(placa="EXC1A18", marca_modelo="Gol", km_atual=30_000)
    resposta = usuario_logado.post(
        f"/km/registrar/{veiculo.pk}/",
        {"data_leitura": HOJE.isoformat(), "km": "31000", "mes": f"{HOJE:%Y-%m}"},
        HTTP_HX_REQUEST="true",
    )

    assert resposta.status_code == 200
    html = resposta.content.decode()
    assert "A leitura foi gravada" in html
    assert RegistroKm.objects.filter(veiculo=veiculo, km=31_000).exists()
