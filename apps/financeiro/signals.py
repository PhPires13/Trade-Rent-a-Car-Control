from contextlib import contextmanager
from contextvars import ContextVar

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.alocacoes.models import Alocacao

from .models import AplicacaoRecebimento, Cobranca, MovimentacaoCaucao


@receiver(post_save, sender=Alocacao)
def criar_caucao_da_alocacao(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """Alocação ativa com caução acordada → registro de caução existe (docs.md §4.4).

    Vale também para a caução combinada depois, em "Editar valores" (C01): sem o
    registro não havia onde lançar o depósito nem acerto na devolução.
    abrir_caucao é idempotente (get_or_create).
    """
    if raw or not instance.caucao_valor or instance.status != Alocacao.Status.ATIVA:
        return
    if update_fields is not None and "caucao_valor" not in update_fields:
        return  # gravação parcial (ex.: encerrar) não mexe na caução
    from .services import abrir_caucao

    abrir_caucao(instance)


#: ligado enquanto um serviço apaga pagamentos e ele mesmo recalcula as cobranças
_recalculo_manual = ContextVar("recalculo_manual", default=False)


@contextmanager
def sem_recalculo_automatico():
    """Desliga o recálculo destes sinais dentro do bloco.

    Para os serviços que já recalculam a cobrança logo depois de apagar
    (desfazer recebimento/caução): sem isso a cobrança era relida e gravada
    duas vezes, com uma versão intermediária falsa no histórico.
    """
    marca = _recalculo_manual.set(True)
    try:
        yield
    finally:
        _recalculo_manual.reset(marca)


def _recalcular_cobranca(cobranca_id):
    if _recalculo_manual.get():
        return
    cobranca = Cobranca.objects.filter(pk=cobranca_id).first()
    if cobranca:
        cobranca.atualizar_status()


@receiver(post_delete, sender=AplicacaoRecebimento)
def reabrir_cobranca_sem_aplicacao(sender, instance, **kwargs):
    """Apagar recebimento/aplicação no Admin devolve o saldo à cobrança (docs.md §4.3)."""
    _recalcular_cobranca(instance.cobranca_id)


@receiver(post_delete, sender=MovimentacaoCaucao)
def reabrir_cobranca_sem_desconto(sender, instance, **kwargs):
    """Apagar um desconto de caução no Admin devolve o saldo à cobrança quitada."""
    if instance.cobranca_id:
        _recalcular_cobranca(instance.cobranca_id)
