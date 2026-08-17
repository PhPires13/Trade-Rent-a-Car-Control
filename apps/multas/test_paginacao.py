"""Paginação da lista de multas — fim do corte silencioso em 200 (auditoria UX)."""

from datetime import date, timedelta
from decimal import Decimal

from apps.frota.models import Veiculo
from apps.multas.models import Multa


def test_lista_de_multas_paginada(usuario_logado, db):
    veiculo = Veiculo.objects.create(placa="QXQ6C10", marca_modelo="Gol")
    Multa.objects.bulk_create(
        [
            Multa(
                veiculo=veiculo,
                data_infracao=date(2026, 1, 1) + timedelta(days=i),
                valor=Decimal("100.00"),
                descricao=f"Infração {i}",
            )
            for i in range(60)
        ]
    )
    resposta = usuario_logado.get("/multas/")
    assert resposta.context["pagina"].paginator.count == 60
    assert len(resposta.context["multas"]) == 50
    html = resposta.content.decode()
    assert "de 60 multas" in html
    assert "pagina=2" in html
    pagina2 = usuario_logado.get("/multas/", {"pagina": "2"})
    assert len(pagina2.context["multas"]) == 10
