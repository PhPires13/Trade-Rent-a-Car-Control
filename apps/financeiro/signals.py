from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.alocacoes.models import Alocacao

from .models import AplicacaoRecebimento, Cobranca, MovimentacaoCaucao


@receiver(post_save, sender=Alocacao)
def criar_caucao_da_alocacao(sender, instance, created, **kwargs):
    """Alocação criada com caução acordada → registro de caução nasce (docs.md §4.4)."""
    if created and instance.caucao_valor:
        from .services import abrir_caucao

        abrir_caucao(instance)


def _recalcular_cobranca(cobranca_id):
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
