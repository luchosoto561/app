from __future__ import annotations
import re
import unicodedata
from typing import Optional

"""
normaliza texto, dispara intencion por normas deterministas y devuelve la intencion o none
"""
# /services/intent_detector.py
# -*- coding: utf-8 -*-
"""
Detección determinista de intención a partir de palabras clave.
- Normaliza el texto (minúsculas + sin tildes).
- Busca palabras/expresiones clave por intención.
- Retorna el nombre de la intención o None.
"""


# ---------------------------
# 1) Listas de palabras clave
#    (agregá/ajustá según tu dominio)
# ---------------------------

# Crear evento
CREATE_KEYWORDS = [
    r"\bcrear\b",
    r"\bagendar\b",
    r"\bprogramar\b",
    r"\bcrea(r)?\b",
    r"\bagenda(r|me)?\b",
    r"\bpon(e|er)\s+(en\s+)?(el\s+)?(calendario|agenda)\b",
]

# Eliminar/cancelar evento
CANCEL_KEYWORDS = [
    r"\bcancel(ar|a|a(r)?lo)?\b",
    r"\beliminar\b",
    r"\bborrar\b",
    r"\banula(r)?\b",
    r"\bdar\s+de\s+baja\b",
]

# Actualizar/mover evento
UPDATE_KEYWORDS = [
    r"\bmover\b",
    r"\bposponer\b",
    r"\bcambiar\b",
    r"\breprogramar\b",
    r"\bmodificar\b",
    r"\bpasar\b",              # "pasalo para mañana"
]

# Chequear disponibilidad (free/busy)
CHECK_AVAILABILITY_KEYWORDS = [
    r"\bestoy\s+libre\b",
    r"\bten(go|es)\s+(libre|ocupado)\b",
    r"\bdisponibilidad\b",
    r"\best(as|oy)\s+libre\b",
    r"\bhay\s+hueco\b",
    r"\bpuedo\s+el\b",         # "puedo el viernes a las 16?"
    r"\bme\s+queda\s+bien\b",
]


# ---------------------------
# 2) Normalización
# ---------------------------

def _strip_accents(s: str) -> str:
    """Quita tildes/acentos (NFD) y retorna solo caracteres base."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )

def _normalize(msg: str) -> str:
    """Minúsculas + sin tildes + trim."""
    return _strip_accents(msg).lower().strip()


# ---------------------------
# 3) Clasificador simple
# ---------------------------

def detect_intent(text: str) -> Optional[str]:
    """
    Recibe un string, lo normaliza y, si contiene alguna palabra clave
    de una intención, devuelve:
      - "CREATE_EVENT"
      - "CANCEL_EVENT"
      - "UPDATE_EVENT"
      - "CHECK_AVAILABILITY"
    Si no matchea ninguna, retorna None.
    """
    t = _normalize(text)

    # Orden de chequeo: ajustalo si querés priorizar alguna intención.
    for pat in CREATE_KEYWORDS:
        if re.search(pat, t):
            return "CREATE_EVENT"

    for pat in CANCEL_KEYWORDS:
        if re.search(pat, t):
            return "CANCEL_EVENT"

    for pat in UPDATE_KEYWORDS:
        if re.search(pat, t):
            return "UPDATE_EVENT"

    for pat in CHECK_AVAILABILITY_KEYWORDS:
        if re.search(pat, t):
            return "CHECK_AVAILABILITY"

    return None
