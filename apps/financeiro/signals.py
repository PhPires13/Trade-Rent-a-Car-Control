from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.alocacoes.models import Alocacao


@receiver(post_save, sender=Alocacao)
def criar_caucao_da_alocacao(sender, instance, created, **kwargs):
    """Alocação criada com caução acordada → registro de caução nasce (docs.md §4.4)."""
    if created and instance.caucao_valor:
        from .services import abrir_caucao

        abrir_caucao(instance)
