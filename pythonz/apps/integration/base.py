from collections import defaultdict
from enum import unique
from typing import Dict, Type, Optional, List

from django.db import models


class RemoteSource:
    """База для удалённых источников данных."""

    realm: str = ''
    """Область, к которой привязан источник."""

    active: bool = True
    """Показатель активности источника."""

    alias: str = ''
    """Псевдоним источника (краткий лат.)."""

    title: str = ''
    """Название источника."""

    registry: dict = defaultdict(dict)
    """Зарегистрированные источники."""

    def __init_subclass__(cls):
        super().__init_subclass__()

        alias = cls.alias

        if alias:
            cls.registry[cls.realm][alias] = cls

    @classmethod
    def get_sources(cls) -> Dict[str, Type['RemoteSource']]:
        """Возвращает словарь с источниками, зарегистрированными для области."""
        return cls.registry[cls.realm]

    @classmethod
    def get_source(cls, alias: str) -> Optional[Type['RemoteSource']]:
        """Возвращает класс источника по псевдониму.

        :param alias: Псевдоним источника.

        """
        return cls.get_sources().get(alias)

    @classmethod
    def get_enum(cls) -> Type[models.TextChoices]:
        """Возвращает перечисление источников для модели."""

        enum = unique(models.TextChoices('Source', [
            (alias, (alias, source_cls.title))
            for alias, source_cls in cls.get_sources().items()
        ]))

        return enum

    def fetch_list(self) -> List[dict]:
        """Возвращает словарь с данными записей, полученных из внешнего
        источника.

        """
        raise NotImplementedError  # pragma: nocover
