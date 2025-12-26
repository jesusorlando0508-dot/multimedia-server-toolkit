import logging
from typing import List

from src.translator.translator import session, limpiar_traduccion, config


class DeepLTranslator:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = "https://api-free.deepl.com/v2/translate"

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        try:
            resp = session.post(self.endpoint, data={"auth_key": self.api_key, "text": text, "target_lang": "ES"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            translations = data.get("translations") or []
            if not translations or not isinstance(translations, list):
                return text
            first = translations[0] or {}
            txt = first.get("text") if isinstance(first, dict) else None
            return limpiar_traduccion(txt or text)
        except Exception as e:
            logging.warning("DeepL translation failed: %s", e)
            return text

    def translate_batch(self, texts: list) -> List[str]:
        resultados = []
        for t in texts:
            resultados.append(self.translate(t))
        return resultados

__all__ = ["DeepLTranslator"]
