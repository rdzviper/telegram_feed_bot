"""Загрузчик плагинов сырого роутинга.

load_plugins() импортирует все модули в plugins/, собирает объекты PLUGIN.
Нет модулей / нет PLUGIN — пустой список (функционала нет). Плагин можно
выключить без удаления файла: env PLUGIN_<NAME>_ENABLED=0.
"""
import importlib
import logging
import os
import pkgutil

logger = logging.getLogger(__name__)


def load_plugins() -> list:
    plugins = []
    pkg_dir = os.path.dirname(__file__)
    for mod in pkgutil.iter_modules([pkg_dir]):
        if mod.name == "base" or mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue
        flag = os.environ.get(f"PLUGIN_{plugin.name.upper()}_ENABLED", "1")
        if flag.lower() in ("0", "false", "no"):
            logger.info("plugin %s disabled via env", plugin.name)
            continue
        plugins.append(plugin)
        logger.info("plugin discovered: %s -> %s", plugin.name, plugin.source_channels)
    return plugins
