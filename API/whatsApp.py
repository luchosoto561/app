from __future__ import annotations
import os
import time
from urllib.parse import quote_plus
from typing import Any, Dict, List, Optional, Tuple 


from fastapi import APIRouter, Request, Response, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from services.google_auth_store import get_google_credentials
from services.whatsapp import send_text  # usa tu wrapper a la API de Meta
from services.google_auth_store import ensure_access
from sqlalchemy import select
from models.conversation_state import ConversationState
from services.intent_detector import detect_intent
from whatsApp import resolver_evento_id
from services.slots_extractor import extraer_slots


router = APIRouter()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "VERIFICATION_123")

# Anti-spam muy simple en memoria (MVP)
_LAST_LINK_SENT_AT: dict[str, float] = {}
LINK_COOLDOWN_SECONDS = 120  # no re-enviar link más de 1 vez cada 2 min por número

# --- Campos mínimos por intención (V1) ---
# Nota: nombres de slots en español, simples y consistentes con tu slots_json.

MIN_REQUIRED_SLOTS = {
    "crear": {
        "obligatorios": ["titulo", "inicio"],
        # Al menos uno de estos debe estar presente; el extractor valida esta alternativa.
        "al_menos_uno_de": [["fin", "duracion"]],
    },
    "consultar_disponibilidad": {
        # Ventana temporal a consultar
        "obligatorios": ["desde", "hasta"],
    },
    "actualizar": {
        # Identificación inequívoca del evento (id, o selección que resuelva a uno)
        "obligatorios": ["selector_evento", "cambios"],  # "cambios" debe contener al menos un campo válido
    },
    "cancelar": {
        "obligatorios": ["selector_evento"],
    },
}

# (Opcional útil para copy UX al usuario)
INTENT_LABEL = {
    "crear": "Crear",
    "consultar_disponibilidad": "Consultar disponibilidad",
    "actualizar": "Actualizar",
    "cancelar": "Eliminar",
}

# (Opcional: breve ayuda por intención, por si querés mostrar “qué falta” con texto claro)
SLOT_LABEL = {
    "titulo": "título",
    "inicio": "inicio (fecha y hora)",
    "fin": "fin (fecha y hora)",
    "duracion": "duración",
    "desde": "desde (fecha y hora)",
    "hasta": "hasta (fecha y hora)",
    "selector_evento": "identificador del evento",
    "cambios": "cambios a aplicar",
}


async def aplicar_extraccion_de_slots(
    *,
    intent_actual: str,
    message_text: str,
    state_slots: Dict[str, Any],
    pending_message: Optional[str],
    calendar_client: Any,
    timezone: str = "America/Argentina/Buenos_Aires",
) -> Tuple[Dict[str, Any], List[str], bool]:
    """
    Orquesta la actualización de slots para la intención vigente en este turno.

    - Combina `pending_message` (si existe) con `message_text`.
    - Llama a `extraer_slots` para proponer valores normalizados.
    - Integra propuestas a `state_slots` respetando la política V1:
        * completar campos faltantes; no sobreescribir valores ya confirmados.
        * en 'actualizar', mergear el objeto `cambios` si el extractor lo aporta.
    - Si la intención es 'actualizar' o 'cancelar' e hiciste extracción de criterios
      descriptivos, intenta resolver el `event_id` vía `resolver_evento_id`.
    - Calcula y devuelve la lista de campos mínimos faltantes para poder ejecutar
      la acción correspondiente.
    
    Retorna:
        (slots_actualizados, faltantes, consumio_pending)
    """
    # 1) Preparar texto a procesar y marcar si consumimos el pending_message.
    consumio_pending = False
    texto_turno = (pending_message.strip() + "\n" + message_text.strip()).strip() if pending_message else message_text.strip()
    if pending_message:
        consumio_pending = True

    # 2) Ejecutar extracción pura (sin I/O) sobre el texto del turno.
    #    El extractor NO resuelve IDs ni llama a Calendar: solo propone datos.
    slots_propuestos, criterios_evento, cambios = extraer_slots(
        intent=intent_actual,
        texto=texto_turno,
        slots_actuales=state_slots,
        timezone=timezone,
    )

    # 3) Merge determinista de propuestas → slots del estado (no pisar confirmados).
    slots_actualizados: Dict[str, Any] = dict(state_slots) if state_slots else {}

    # 3.a) Completar campos nuevos o vacíos (sin sobrescribir existentes no vacíos).
    if isinstance(slots_propuestos, dict):
        for k, v in slots_propuestos.items():
            if v is None:
                continue
            if k not in slots_actualizados or slots_actualizados.get(k) in (None, "", [], {}):
                slots_actualizados[k] = v

    # 3.b) En 'actualizar', integrar `cambios` como dict mergeable.
    if intent_actual == "actualizar" and isinstance(cambios, dict) and cambios:
        base = slots_actualizados.get("cambios")
        if not isinstance(base, dict):
            slots_actualizados["cambios"] = dict(cambios)
        else:
            # Merge superficial: completa keys ausentes; no pisa valores existentes.
            for ck, cv in cambios.items():
                if ck not in base or base.get(ck) in (None, "", [], {}):
                    base[ck] = cv

    # 4) Si la intención requiere identificar un evento existente, intentar resolver event_id.
    #    El extractor entrega criterios descriptivos; acá los usamos para buscar.
    if intent_actual in ("actualizar", "cancelar") and "event_id" not in slots_actualizados:
        if isinstance(criterios_evento, dict) and criterios_evento: 
            event_id, opciones = await resolver_evento_id(
                criterios=criterios_evento,
                calendar_client=calendar_client,
                timezone=timezone,
                max_opciones=3,
            )
            if event_id:
                slots_actualizados["event_id"] = event_id
            else:
                # Podés exponer las opciones al caller guardándolas transitoriamente en slots.
                # El caller decidirá si las muestra y cómo pide la elección.
                if opciones:
                    slots_actualizados["opciones_evento"] = opciones

    # 5) Calcular mínimos faltantes para poder ejecutar la acción.
    #    Política V1: chequear explícitamente por intención.
    faltantes: List[str] = []

    if intent_actual == "crear":
        # Obligatorios: inicio + (fin o duracion)
        if not slots_actualizados.get("inicio"):
            faltantes.append("inicio")
        tiene_fin = bool(slots_actualizados.get("fin"))
        tiene_duracion = bool(slots_actualizados.get("duracion"))
        if not (tiene_fin or tiene_duracion):
            faltantes.append("fin/duracion")  # copy compacto para UX

        # Título como obligatorio “blando” en tu V1 si así lo definiste.
        if not slots_actualizados.get("titulo"):
            faltantes.append("titulo")

    elif intent_actual == "consultar_disponibilidad":
        if not slots_actualizados.get("desde"):
            faltantes.append("desde")
        if not slots_actualizados.get("hasta"):
            faltantes.append("hasta")

    elif intent_actual == "actualizar":
        # Siempre necesitás identificar el evento y al menos un cambio concreto.
        if not slots_actualizados.get("event_id"):
            faltantes.append("event_id")
        cambios_dict = slots_actualizados.get("cambios")
        if not isinstance(cambios_dict, dict) or len(cambios_dict) == 0:
            faltantes.append("cambios")

    elif intent_actual == "cancelar":
        if not slots_actualizados.get("event_id"):
            faltantes.append("event_id")

    # 6) Limpiar el pending_message solo si lo consumimos efectivamente.
    #    (La limpieza en DB/estado la hace el caller usando el flag `consumio_pending`.)
    return slots_actualizados, faltantes, consumio_pending

async def resolver_evento_id(
    *,
    criterios: Dict[str, Any],
    calendar_client: Any,
    timezone: str = "America/Argentina/Buenos_Aires",
    max_opciones: int = 3,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Resuelve un `event_id` a partir de criterios descriptivos (fecha/ventana, fragmento
    de título, asistentes, hora aproximada, etc.). Si la búsqueda produce ambigüedad,
    devuelve una lista corta de opciones para que el usuario elija.

    Parámetros:
      criterios: Diccionario con las pistas normalizadas para identificar el evento.
      calendar_client: Cliente/servicio para consultar Google Calendar.
      timezone: Zona horaria para interpretar rangos y fechas.
      max_opciones: Límite de alternativas a devolver si hay múltiples coincidencias.

    Retorna:
      (event_id, opciones)
        - event_id: string si se obtuvo una coincidencia única; None si no es unívoco.
        - opciones: lista (máx. `max_opciones`) con candidatos {event_id, summary, start, end}
                    cuando `event_id` es None; lista vacía si no hubo resultados.
    """


@router.get("/webhook")
async def verify(request: Request):
    """verificacion inicial del webhook de whatsapp -> mostramos que somos nosotros"""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge or "", status_code=200)
    return Response(status_code=403)


@router.post("/webhook")
async def receive(request: Request, session: AsyncSession = Depends(get_session)):
    """
    Punto donde Meta envía cada mensaje real.
    Debemos responder 200 rápido. El procesamiento puede ser best-effort.
    """
    payload = await request.json()

    # 1) Extraer teléfono (WhatsApp Cloud API estructura típica)
    #    entry[0].changes[0].value.messages[0].from
    phone: str | None = None
    try:
        #payload.get("entry", []) te devuelve el valor que hay en la clave entry de payload, si no existe te devuelve el valor por defecto que pasaste []. y entry es el elemento en la posicion cero de esa lista
        entry = payload.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if messages:
            phone = messages[0].get("from")  # E.164 sin '+', ej: '549221...'
            
    except Exception:
        phone = None

    if not phone:
        # No sabemos a quién responder; igual 200 para no reintentos. 
        return Response(status_code=200)

    # 2) Decidir qué hacer con acceso a Calendar (vigente / refresh / re-consent / transitorio)
    decision = await ensure_access(session, phone=phone)
    print(f"la decision es {decision}", flush=True)
    # 2a) Si hay que mandar link de consentimiento (primera vez o re-consent)
    if decision.get("action") == "send_link":
        now = time.monotonic()
        last = _LAST_LINK_SENT_AT.get(phone, 0.0)

        # Anti-spam: no re-enviar si está en cooldown
        if now - last < LINK_COOLDOWN_SECONDS:
            await send_text(
                to_phone=phone,
                body="Te envié el enlace para conectar Google Calendar hace un momento. Revísalo y tocá para continuar ✅",
            )
            return Response(status_code=200)

        base = str(request.base_url).rstrip("/")
        prompts = decision.get("prompts", {})
        force = "1" if prompts.get("force_consent") else "0"
        select = "1" if prompts.get("select_account") else "0"
        start_link = f"{base}/auth/google/start?phone={quote_plus(phone)}&force_consent={force}&select_account={select}"

        # Copy de UX según motivo
        reason = decision.get("reason", "")
        if reason == "no_credentials":
            msg = (
                "Para conectar tu Google Calendar y poder ayudarte con tus eventos, "
                f"tocá este enlace seguro: {start_link}"
            )
        elif reason == "refresh_invalid_grant":
            msg = (
                "Necesito que vuelvas a autorizar el acceso a tu Google Calendar "
                f"para seguir ayudándote: {start_link}"
            )
        else:
            msg = f"Por favor, conectá tu Google Calendar acá: {start_link}"

        await send_text(to_phone=phone, body=msg)
        _LAST_LINK_SENT_AT[phone] = now
        return Response(status_code=200)

    # 2b) Si hubo problema transitorio al refrescar, avisamos y cortamos (no spameamos link)
    if decision.get("status") == "need_refresh":
        await send_text(
            to_phone=phone,
            body="Tuve un problema técnico con Google. Probemos de nuevo en un rato ✋",
        )
        return Response(status_code=200)

    # -----------------------------------------------------------------------------------------------------------------------
    # 2c) Si está todo OK (token vigente o recién refrescado), seguí con la intención del usuario
    
    # 2c.1) Extraer texto del mensaje (si no viene, tratamos como cadena vacía)
    message_text = ""
    try:
        msg_obj = messages[0]
        if msg_obj.get("type") == "text":
            message_text = (msg_obj.get("text", {}) or {}).get("body", "") or ""
        else:
            # Otros tipos (audio, imagen, etc.) por ahora no aportan a intención
            message_text = ""
    except Exception:
        message_text = ""

    # 2c.2) Obtener/crear estado de conversación del usuario
    result = await session.execute(
        select(ConversationState).where(ConversationState.whatsapp_phone == phone)
    )
    state: ConversationState | None = result.scalar_one_or_none()
    if state is None:
        state = ConversationState(
            whatsapp_phone=phone,
            intent_actual=None,
            slots_json={},            # empezamos vacío
            pending_intent=None,
            pending_message=None,
        )
        session.add(state)
        # se guardan los cambios realmente en la base de datos
        await session.commit()
        # vuelve a leer desde la base esa fila y actualiza el objeto en memoria con lo que quedo finalmente
        await session.refresh(state)

    # 2c.3) Si hay un cambio de intención pendiente, resolvemos con un sí/no simple
    if state.pending_intent:
        normalized = (message_text or "").strip().lower()
        affirmatives = {"si", "sí", "dale", "ok", "okay", "correcto", "affirmative", "de una"}
        negatives   = {"no", "mejor no", "nop"}

        if normalized in affirmatives:
            # Confirmó el cambio: aplicamos nueva intención y reiniciamos slots
            state.intent_actual = state.pending_intent
            state.slots_json = {}
            await session.commit()

            # Procesar ahora el pending_message + mensaje actual
            calendar_client = None  # o tu client real si aplica
            slots_actualizados, faltantes, consumio_pending = await aplicar_extraccion_de_slots(
                intent_actual=state.intent_actual,
                message_text=message_text,
                state_slots=state.slots_json or {},
                pending_message=state.pending_message,
                calendar_client=calendar_client,
                timezone="America/Argentina/Buenos_Aires",
            )
            state.slots_json = slots_actualizados
            state.pending_intent = None
            if consumio_pending:
                state.pending_message = None
            await session.commit()

            await send_text(
                to_phone=phone,
                body=f"Perfecto, cambiamos a *{state.intent_actual.replace('_', ' ').title()}*. Contame los detalles."
            )
            return Response(status_code=200)

        elif normalized in negatives:
            # Rechazó el cambio: limpiamos pendientes y seguimos con la intención actual
            state.pending_intent = None
            state.pending_message = None
            await session.commit()
            await send_text(
                to_phone=phone,
                body=f"Seguimos con *{(state.intent_actual or 'ninguna').replace('_', ' ').title()}*. ¿Me pasás los detalles?"
            )
            return Response(status_code=200)
        else:
            # No respondió claramente sí/no: repreguntamos
            await send_text(
                to_phone=phone,
                body=f"¿Cambiamos a *{state.pending_intent.replace('_', ' ').title()}*? Respondé *sí* o *no*."
            )
            return Response(status_code=200)

    # 2c.4) Detectar intención en el mensaje actual
    detected = detect_intent(message_text)

    if state.intent_actual is None:
        # No hay intención vigente
        if detected is None:
            await send_text(
                to_phone=phone,
                body="¿Qué querés hacer? *crear*, *consultar disponibilidad*, *actualizar* o *cancelar*."
            )
            return Response(status_code=200)

        # Seteamos intención y avanzamos (slots vienen después)
        state.intent_actual = detected
        
        # actualizamos slots segun la intencion
         
        # (opcional) si vas a actualizar/cancelar, obtené un client real; si es crear/consultar podés pasar None
        calendar_client = None

        slots_actualizados, faltantes, consumio_pending = await aplicar_extraccion_de_slots(
            intent_actual=state.intent_actual,
            message_text=message_text,
            state_slots=state.slots_json or {},
            pending_message=None,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )
        
        state.slots_json = slots_actualizados
        await session.commit()
        
        await send_text(
            to_phone=phone,
            body=f"Listo, vamos con *{detected.replace('_', ' ').title()}*. Contame los detalles."
        )
        return Response(status_code=200)

    else:
        # Hay intención vigente
        if detected and detected != state.intent_actual:
            # Propuesta de cambio: guardamos pendiente y pedimos confirmación
            state.pending_intent = detected
            state.pending_message = message_text
            await session.commit()
            await send_text(
                to_phone=phone,
                body=f"Estás en *{state.intent_actual.replace('_', ' ').title()}*. ¿Querés cambiar a *{detected.replace('_', ' ').title()}*? (sí/no)"
            )
            return Response(status_code=200)
        
        # (opcional) si la intención es 'actualizar' o 'cancelar', prepará el client real acá
        calendar_client = None

        slots_actualizados, faltantes, consumio_pending = await aplicar_extraccion_de_slots(
            intent_actual=state.intent_actual,
            message_text=message_text,
            state_slots=state.slots_json or {},
            pending_message=state.pending_message,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )
        state.slots_json = slots_actualizados
        if consumio_pending:
            state.pending_message = None
        await session.commit()
        
        # Sin cambio de intención: seguimos con la actual → próximamente: extracción de slots
        # Por ahora, pedimos datos de manera genérica hasta que conectes el slots_extractor
        await send_text(
            to_phone=phone,
            body=f"Continuemos con *{state.intent_actual.replace('_', ' ').title()}*. Decime fecha y hora (y lo que tengas) y lo proceso."
        )
        return Response(status_code=200)
    
    
     