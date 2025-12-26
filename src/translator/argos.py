import logging
import importlib
import importlib.util
from typing import List
from src.core.config import config

logger = logging.getLogger(__name__)


class ArgosTranslator:
    """Wrapper for Argos Translate integrated with the project's translator flow.

    Features:
    - Lazy-imports `argostranslate`.
    - Optionally installs a missing en-><target> model from the argospm index
      when `argos_auto_install_models` is true in config.
    - Exposes `ensure_loaded()`, `ensure_loaded_safe()`, `translate()` and `translate_batch()`
      compatible with `run_batched_translation`.
    """

    def __init__(self):
        self._loaded = False
        self._package = None
        self._translate = None

    def ensure_loaded(self):
        if self._loaded:
            return

        spec = importlib.util.find_spec('argostranslate')
        if spec is None:
            raise ImportError('argostranslate package not installed')

        try:
            import argostranslate.package as package
            import argostranslate.translate as translate
        except Exception as e:
            logger.debug('Failed importing argostranslate modules: %s', e, exc_info=True)
            raise

        self._package = package
        self._translate = translate

        target = (config.get('translator_target_lang') or 'es')
        try:
            installed = []
            try:
                installed = translate.get_installed_languages() or []
            except Exception:
                installed = []

            pair_available = False
            if installed:
                has_en = any(getattr(l, 'code', None) == 'en' for l in installed)
                has_target = any(getattr(l, 'code', None) == target for l in installed)
                pair_available = has_en and has_target

            auto_install = bool(config.get('argos_auto_install_models', False))
            if not pair_available and auto_install:
                try:
                    package.update_package_index()
                    available = package.get_available_packages() or []
                    candidate = next((p for p in available if getattr(p, 'from_code', None) == 'en' and getattr(p, 'to_code', None) == target), None)
                    if candidate is None:
                        candidate = next((p for p in available if getattr(p, 'from_code', None) == 'en'), None)
                    if candidate is not None:
                        try:
                            path = candidate.download()
                            package.install_from_path(path)
                            installed = translate.get_installed_languages() or []
                            has_en = any(getattr(l, 'code', None) == 'en' for l in installed)
                            has_target = any(getattr(l, 'code', None) == target for l in installed)
                            pair_available = has_en and has_target
                        except Exception as ie:
                            logger.debug('Argos: failed to download/install package %s: %s', getattr(candidate, '__dict__', candidate), ie)
                except Exception as ie:
                    logger.debug('Argos: package index install failed: %s', ie)

        except Exception:
            pass

        self._loaded = True

    def ensure_loaded_safe(self) -> bool:
        try:
            self.ensure_loaded()
            return True
        except Exception:
            return False

    def translate(self, text: str) -> str:
        if not text:
            return ''
        try:
            if not self._loaded:
                return text

            tr_mod = self._translate
            if tr_mod is None:
                return text

            target = (config.get('translator_target_lang') or 'es')
            try:
                return tr_mod.translate(text, 'en', target)
            except Exception:
                try:
                    langs = tr_mod.get_installed_languages()
                    from_lang = next((l for l in langs if getattr(l, 'code', None) == 'en'), None)
                    to_lang = next((l for l in langs if getattr(l, 'code', None) == target), None)
                    if from_lang and to_lang:
                        trans = from_lang.get_translation(to_lang)
                        if trans:
                            return trans.translate(text)
                except Exception:
                    pass
            return text
        except Exception:
            return text

    def translate_batch(self, texts: List[str]) -> List[str]:
        if not texts:
            return []
        return [self.translate(t) for t in texts]

    def install_package_for_target(self, from_code: str = 'en', to_code: str | None = None, ui_queue=None, max_attempts: int = 3) -> dict:
        """Try to find and install a package for the requested language pair.

        Returns a dict: {"success": bool, "package": candidate or None, "path": str|None, "error": str|None, "elapsed": float}

        If `to_code` is None the translator target in config is used.
        If `ui_queue` is provided, progress messages will be emitted as (`debug_process`, msg).
        """
        import time
        start = time.time()
        result = {"success": False, "package": None, "path": None, "error": None, "elapsed": None}
        try:
            to_code = to_code or (config.get('translator_target_lang') or 'es')
            try:
                if ui_queue is not None:
                    try:
                        ui_queue.put(("debug_process", f"Argos: actualizando índice de paquetes..."))
                    except Exception:
                        pass
                self.ensure_loaded()
            except Exception:
                # even if ensure_loaded fails, try to import package module directly
                try:
                    import importlib.util
                    spec = importlib.util.find_spec('argostranslate.package')
                    if spec is None:
                        raise ImportError('argostranslate.package not available')
                    import argostranslate.package as package
                except Exception as e:
                    result['error'] = f'Import error: {e}'
                    return result

            package = self._package
            translate = self._translate
            attempt = 0
            candidate = None
            last_err = None
            while attempt < max_attempts:
                attempt += 1
                try:
                    package.update_package_index() # type: ignore
                    available = package.get_available_packages() or [] # type: ignore
                    # prefer direct from_code -> to_code
                    candidate = next((p for p in available if getattr(p, 'from_code', None) == from_code and getattr(p, 'to_code', None) == to_code), None)
                    if candidate is None:
                        # fallback to any from_code == from_code
                        candidate = next((p for p in available if getattr(p, 'from_code', None) == from_code), None)
                    if candidate is None:
                        last_err = 'No candidate package found in index'
                        if ui_queue is not None:
                            ui_queue.put(("debug_process", f"Argos: ningún paquete disponible para {from_code}->{to_code} (intento {attempt})"))
                        continue
                    # download
                    if ui_queue is not None:
                        ui_queue.put(("debug_process", f"Argos: descargando paquete {getattr(candidate,'name',str(candidate))} ..."))
                    path = candidate.download()
                    if ui_queue is not None:
                        ui_queue.put(("debug_process", f"Argos: instalando desde {path} ..."))
                    package.install_from_path(path) #type: ignore
                    result['success'] = True
                    result['package'] = candidate
                    result['path'] = path
                    break
                except Exception as e:
                    last_err = str(e)
                    if ui_queue is not None:
                        ui_queue.put(("debug_process", f"Argos: intento {attempt} falló: {e}"))
                    continue
            if not result['success']:
                result['error'] = last_err or 'No package installed'
        except Exception as e:
            result['error'] = str(e)
        finally:
            try:
                result['elapsed'] = time.time() - start
            except Exception:
                result['elapsed'] = None
            if ui_queue is not None:
                try:
                    ui_queue.put(("debug_process", f"Argos: instalación finalizada, success={result.get('success')} elapsed={result.get('elapsed'):.1f}s"))
                except Exception:
                    pass
        return result

