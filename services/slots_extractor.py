"""

"""
from typing import Any, Dict, Optional, Tuple

def extraer_slots(
    *,
    intent: str,
    texto: str,
    slots_actuales: Dict[str, Any],
    timezone: str = "America/Argentina/Buenos_Aires",
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Extrae y normaliza información desde lenguaje natural hacia slots estructurados
    según la intención. No realiza I/O externo ni resuelve IDs; solo parsea.

    Comportamiento por intención:
      - 'crear' / 'consultar_disponibilidad': retorna slots normalizados (p. ej., inicio/fin,
        o desde/hasta). Los otros dos retornos serán None.
      - 'cancelar': retorna slots (si ya existieran) y `criterios_evento` para que otro
        módulo resuelva el `event_id`. `cambios` será None.
      - 'actualizar': retorna slots (si ya existieran), `criterios_evento` y también
        `cambios` (los campos a modificar, ya interpretados desde el texto).

    Parámetros:
      intent: Intención vigente.
      texto: Texto combinado a procesar en este turno (puede incluir pending + actual).
      slots_actuales: Estado de slots previo para permitir merges idempotentes.
      timezone: Zona horaria para interpretar/normalizar fechas y horas.

    Retorna:
      (slots_propuestos, criterios_evento, cambios)
        - slots_propuestos: dict con nuevas propuestas de valores (mergeable con `slots_actuales`).
        - criterios_evento: dict con pistas para identificar el evento (solo en actualizar/cancelar).
        - cambios: dict con modificaciones solicitadas (solo en actualizar).
    """