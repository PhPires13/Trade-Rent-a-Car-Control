"""KM fora do normal na vistoria, na devolução e no fim da troca (decisão D27).

Mesma regra do registro mensal de KM (apps/km/correcao.py): ninguém roda mais
que LIMITE_KM_POR_DIA por dia com carro de aplicativo. Acima disso o número
provavelmente tem um dígito a mais — a tela mostra o aviso e só grava quando a
pessoa marca "Confirmo o KM". O teto absoluto (KM_MAXIMO) é recusado sempre.
"""

from apps.formatos import milhar


def suspeita_de_km(km, data, km_base, data_base, rotulo_base):
    """Aviso quando `km` foge do normal em relação à última leitura; None se ok.

    rotulo_base: como a leitura anterior é chamada ("na entrega", "na retirada").
    """
    from apps.km.correcao import LIMITE_KM_POR_DIA, _ha_quanto_tempo  # import local evita ciclo

    if km is None or km_base is None or data is None or data_base is None:
        return None
    rodado = km - km_base
    dias = (data - data_base).days
    if rodado <= LIMITE_KM_POR_DIA * max(dias, 1):
        return None
    por_dia = round(rodado / max(dias, 1))
    return (
        f"Você digitou {milhar(km)} km, mas o carro estava com {milhar(km_base)} km "
        f"{rotulo_base} ({_ha_quanto_tempo(dias)}, em {data_base:%d/%m/%Y}). Seriam "
        f"{milhar(rodado)} km rodados, uns {milhar(por_dia)} km por dia — confira se não "
        f"sobrou um dígito. Se estiver certo, marque “Confirmo o KM” e salve de novo."
    )


def base_da_vistoria(alocacao, veiculo):
    """(km, data, rótulo) da última leitura conhecida do carro nesta alocação."""
    if veiculo.pk != alocacao.veiculo_id:
        troca = (
            alocacao.trocas.filter(veiculo_substituto=veiculo).order_by("-data_retirada").first()
        )
        if troca:
            return troca.km_retirada, troca.data_retirada, "na retirada do substituto"
    return alocacao.km_entrega, alocacao.data_inicio, "na entrega"
