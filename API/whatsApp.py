from __future__ import annotations
import os
import time
from urllib.parse import quote_plus
from typing import Any, Dict, List, Optional, Tuple 
import re
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

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

    # fecha_para_buscar_id es la fecha que se da para buscar el evento por id
    # cambios son las actualizaciones que se le quiere hacer a un evento. Unicamente en actualizar evento.
    slots_propuestos, fecha_para_buscar_id, cambios = extraer_slots(
        intent=intent_actual,
        texto=texto_turno,
        slots_actuales=state_slots,
        timezone=timezone,
    )

    slots_actualizados: Dict[str, Any] = dict(state_slots) if state_slots else {}

    # mergeamos slots de la DB con slots_propuestos
    if isinstance(slots_propuestos, dict):
        for k, v in slots_propuestos.items():
            if v is None:
                continue
            if k not in slots_actualizados or slots_actualizados.get(k) in (None, "", [], {}):
                slots_actualizados[k] = v

    # agrega a slots_actualizados cambios. (mergea con los cambios ya propuestos si es que habia)
    if intent_actual == "actualizar" and isinstance(cambios, dict) and cambios:
        base = slots_actualizados.get("cambios")
        if not isinstance(base, dict):
            slots_actualizados["cambios"] = dict(cambios)
        else:
            # mergea a base con cambios, es decir a los cambios que tenemos en el slots de la DB con los cambios detectados en el mensaje
            for ck, cv in cambios.items():
                if ck not in base or base.get(ck) in (None, "", [], {}):
                    base[ck] = cv

    # si tenemos la fecha cargamos event_id o opciones_evento
    if intent_actual in ("actualizar", "cancelar") and "event_id" not in slots_actualizados:
        if fecha_para_buscar_id:
            slots_actualizados["fecha_objetivo"] = fecha_para_buscar_id 
            event_id, opciones = await resolver_evento_id(
                fecha_id = fecha_para_buscar_id,
                calendar_client=calendar_client,
                timezone=timezone,
            )
            if event_id:
                slots_actualizados["event_id"] = event_id
            else:
                # Podés exponer las opciones al caller guardándolas transitoriamente en slots.
                # El caller decidirá si las muestra y cómo pide la elección.
                if opciones:
                    slots_actualizados["opciones_evento"] = opciones

    # calculamos faltantes para enviar el mensaje de las cosas que faltan para slots_json de la DB
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
    # para actualizar pedimos fecha del evento y al menos un cambio concreto
        if not slots_actualizados.get("fecha_objetivo"):
            faltantes.append("fecha")
        cambios_dict = slots_actualizados.get("cambios")
        if not isinstance(cambios_dict, dict) or not cambios_dict:
            faltantes.append("cambios")

    elif intent_actual == "cancelar":
    # para cancelar pedimos solo la fecha del evento
        crit = slots_actualizados.get("criterios_evento") or {}
        if not crit.get("fecha"):
            faltantes.append("fecha")
   
    return slots_actualizados, faltantes, consumio_pending


async def resolver_evento_id(
    *,
    fecha_id: str,
    calendar_client: Any,
    timezone: str = "America/Argentina/Buenos_Aires",
) -> Tuple[Optional[str], List[Tuple[str, str, str, str]]]:
    """
    Dada una fecha (YYYY-MM-DD) devuelve el event_id único de ese día si hay
    exactamente un evento; si hay 0 o >1, devuelve una lista de opciones para elegir.

    Contrato de salida:
      - Si hay coincidencia única: (event_id, [])
      - En cualquier otro caso: (None, [ (titulo, event_id, inicio_str, fin_str), ... ])

    Expectativa sobre `calendar_client`:
      Debe exponer un método async que liste eventos en un rango:
        await calendar_client.list_events(
            time_min=<RFC3339>,
            time_max=<RFC3339>,
            single_events=True,
            order_by="startTime",
        ) -> dict con clave "items" (lista de eventos)
      Cada evento debe tener:
        - "id": str
        - "summary": opcional str
        - "status": opcional str (filtramos "cancelled")
        - "start": {"dateTime": RFC3339} o {"date": "YYYY-MM-DD"}
        - "end":   {"dateTime": RFC3339} o {"date": "YYYY-MM-DD"}

    Notas de formateo:
      - inicio_str / fin_str se devuelven en timezone local, formato HH:MM.
      - Si es de día completo, inicio_str="todo el día" y fin_str="".

    """
    # 1) Construir rango [00:00, 24:00) en la zona local
    tz = ZoneInfo(timezone)
    try:
        d = date.fromisoformat(fecha_id)
    except ValueError:
        # Si viene mal, no hay forma de resolver
        return None, []

    start_local = datetime.combine(d, time(0, 0), tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    # RFC3339 para Google
    time_min = start_local.isoformat()
    time_max = end_local.isoformat()

    # 2) Listar eventos del día, ordenados por hora de inicio
    resp = await calendar_client.list_events(
        time_min=time_min,
        time_max=time_max,
        single_events=True,
        order_by="startTime",
    )
    items = (resp or {}).get("items", []) or []

    # 3) Filtrar cancelados
    vivos = [ev for ev in items if ev.get("status") != "cancelled"]

    # 4) Si no hay ninguno → sin ID y sin opciones
    if not vivos:
        return None, []

    # 5) Si hay uno solo → devolver su ID directo
    if len(vivos) == 1:
        return vivos[0].get("id"), []

    # 6) Si hay varios → armar lista de opciones (titulo, id, inicio_str, fin_str)
    def parse_dt(ev_side: dict) -> Optional[datetime]:
        if not ev_side:
            return None
        if "dateTime" in ev_side:
            s = ev_side["dateTime"]
            # Aceptar 'Z' de UTC
            if isinstance(s, str) and s.endswith("Z"):
                s = s.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
            except Exception:
                return None
            return dt.astimezone(tz)
        if "date" in ev_side:
            # Evento de día completo: interpretamos en local
            try:
                dd = date.fromisoformat(ev_side["date"])
            except Exception:
                return None
            return datetime.combine(dd, time(0, 0), tzinfo=tz)
        return None

    opciones: List[Tuple[str, str, str, str]] = []
    for ev in vivos:
        titulo = ev.get("summary") or "(sin título)"
        ev_id = ev.get("id") or ""
        dt_start = parse_dt(ev.get("start", {}))
        dt_end = parse_dt(ev.get("end", {}))

        # Formateo amigable
        if "date" in ev.get("start", {}):  # día completo
            inicio_str = "todo el día"
            fin_str = ""
        else:
            inicio_str = dt_start.strftime("%H:%M") if dt_start else "—"
            fin_str = dt_end.strftime("%H:%M") if dt_end else "—"

        opciones.append((titulo, ev_id, inicio_str, fin_str))

    # Ordenar por inicio (cuando tengamos hora)
    def sort_key(opt: Tuple[str, str, str, str]) -> Tuple[int, str]:
        # Colocar "todo el día" primero; luego por hora textual
        inicio_str = opt[2]
        return (0 if inicio_str == "todo el día" else 1, inicio_str)

    opciones.sort(key=sort_key)
    return None, opciones

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
    
    # Resolver selección de evento (antes de manejar pending_intent / detectar intención)
    if state.slots_json.get("awaiting") == "event_selection":
        
        raw = (message_text or "").strip()
        m = re.search(r"\d+", raw)
        if not m:
            await send_text(to_phone=phone, body="Decime el *número* de la opción (ej.: 1, 2 o 3).")
            return Response(status_code=200)

        idx = int(m.group()) - 1
        opciones = state.slots_json.get("opciones_evento") 
        if not (0 <= idx < len(opciones)):
            await send_text(to_phone=phone, body="Número fuera de rango. Probá con 1, 2 o 3.")
            return Response(status_code=200)

        elegido = opciones[idx]
        # Soportar lista de tuplas (titulo, event_id) o dicts con id
        if isinstance(elegido, (list, tuple)) and len(elegido) >= 2:
            event_id = elegido[1]
        else:
            event_id = elegido.get("event_id") or elegido.get("id")

        if not event_id:
            await send_text(to_phone=phone, body="No pude leer el ID de esa opción. Probá con otra.")
            return Response(status_code=200)

        # Fijar elección y limpiar transitorios
        state.slots_json["event_id"] = event_id
        state.slots_json.pop("opciones_evento", None)
        state.slots_json.pop("awaiting", None)
        await session.commit()

        # Re-evaluar faltantes y ejecutar si ya está todo
        calendar_client = None
        slots_actualizados, faltantes, _ = await aplicar_extraccion_de_slots(
            intent_actual=state.intent_actual,
            message_text="",
            state_slots=state.slots_json or {},
            pending_message=None,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )
        state.slots_json = slots_actualizados
        await session.commit()

        if faltantes:
            await send_text(
                to_phone=phone,
                body=f"Me faltan: *{', '.join(faltantes)}*.\nDecime lo que falta o confirmá para continuar."
            )
            return Response(status_code=200)

        resultado, resumen = await ejecutar_accion_calendar(
            intent=state.intent_actual,
            slots=state.slots_json,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )
        state.intent_actual = None
        state.slots_json = {}
        state.pending_intent = None
        state.pending_message = None
        await session.commit()

        await send_text(to_phone=phone, body=resumen)
        return Response(status_code=200)
    
    
    # 2c.3) Si hay un cambio de intención pendiente, resolvemos con un sí/no simple
    if state.pending_intent:
        normalized = (message_text or "").strip().lower()
        affirmatives = {"si", "sí", "dale", "ok", "okay", "correcto", "affirmative", "de una"}
        negatives   = {"no", "mejor no", "nop"}

        if normalized in affirmatives:
            # Confirmó el cambio: aplicamos nueva intención y reiniciamos slots
            state.intent_actual = state.pending_intent
            state.slots_json = {}
            state.pending_intent = None
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
            if consumio_pending:
                state.pending_message = None
            await session.commit()
            
            opciones = state.slots_json.get("opciones_evento")
            
            if opciones:
                state.slots_json["awaiting"] = "event_selection"
                await session.commit()

                lines = []
                for i, ev in enumerate(opciones, 1):
                    # ev = (titulo, event_id, inicio_str, fin_str)
                    titulo = (ev[0] if len(ev) > 0 else None) or "(sin título)"
                    inicio = (ev[2] if len(ev) > 2 else None) or "—"
                    fin    = (ev[3] if len(ev) > 3 else None) or "—"
                    sep = " a " if fin != "—" else ""
                    lines.append(f"{i}. {titulo} — {inicio}{sep}{fin}")

                await send_text(
                    to_phone=phone,
                    body="Elegí el evento (respondé con el número):\n" + "\n".join(lines),
                )
                return Response(status_code=200)
            
            if faltantes:
                await send_text(
                    to_phone=phone,
                    body=(
                        (f"Me faltan: *{', '.join(faltantes)}*.\n")
                        + "Decime lo que falta o confirmá para continuar."
                    ),
                )
                return Response(status_code=200)
            
            # sin opciones y sin faltantes -> ejecutar y responder
            # (implementar ejecutar_accion_calendar segun tu capa de execute)
            resultado, resumen = await ejecutar_accion_calendar(
                intent = state.intent_actual,
                slots = state.slots_json,
                calendar_client = calendar_client,
                timezone = "America/Argentina/Buenos_Aires",
            )
            
            # limpieza total del estado tras ejecutar con exito
            
            state.intent_actual = None
            state.slots_json = {}
            state.pending_intent = None
            state.pending_message = None
            
            await session.commit()
            
            await send_text(
                to_phone=phone,
                body = resumen
            )
            return Response(status_code=200)
            
        elif normalized in negatives:
            # Rechazó el cambio: limpiamos pendientes y seguimos con la intención actual
            state.pending_intent = None
            state.pending_message = None
            
            # Calcular faltantes según la intención vigente usando los slots actuales
            slots = state.slots_json or {}
            faltantes = []
            if state.intent_actual == "crear":
                if not slots.get("inicio"):
                    faltantes.append("inicio")
                if not (slots.get("fin") or slots.get("duracion")):
                    faltantes.append("fin/duracion")
                if not slots.get("titulo"):
                    faltantes.append("titulo")
            elif state.intent_actual == "consultar_disponibilidad":
                if not slots.get("desde"):
                    faltantes.append("desde")
                if not slots.get("hasta"):
                    faltantes.append("hasta")
            elif state.intent_actual == "actualizar":
                if not slots.get("event_id"):
                    faltantes.append("event_id")
                cambios = slots.get("cambios")
                if not isinstance(cambios, dict) or not cambios:
                    faltantes.append("cambios")
            elif state.intent_actual == "cancelar":
                if not slots.get("event_id"):
                    faltantes.append("event_id")

            await session.commit()
            await send_text(
                to_phone=phone,
                body=(
                    f"Seguimos con *{(state.intent_actual or 'ninguna').replace('_', ' ').title()}*.\n"
                    + (f"Me faltan: *{', '.join(faltantes)}*.\n" if faltantes else "Ya tengo todo lo necesario ✅.\n")
                    + "Decime lo que falta o confirmá para continuar."
                ),
            )
            return Response(status_code=200)
        
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
        
        opciones = state.slots_json.get("opciones_evento")

        if opciones:
            state.slots_json["awaiting"] = "event_selection"
            await session.commit()

            lines = []
            for i, ev in enumerate(opciones, 1):
                titulo = ev.get("summary") or "(sin título)"
                inicio = ev.get("start") or "—"
                fin = ev.get("end") or "—"
                lines.append(f"{i}. {titulo} — {inicio} a {fin}")

            await send_text(
                to_phone=phone,
                body="Elegí el evento (respondé con el número):\n" + "\n".join(lines)
            )
            return Response(status_code=200)

        if faltantes:
            await send_text(
                to_phone=phone,
                body=(f"Me faltan: *{', '.join(faltantes)}*.\n" + "Decime lo que falta o confirmá para continuar.")
            )
            return Response(status_code=200)

        # Sin opciones y sin faltantes → ejecutar y responder
        resultado, resumen = await ejecutar_accion_calendar(
            intent=state.intent_actual,
            slots=state.slots_json,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )

        # Reset total del estado tras ejecutar con éxito
        state.intent_actual = None
        state.slots_json = {}
        state.pending_intent = None
        state.pending_message = None
        await session.commit()

        await send_text(to_phone=phone, body=resumen)
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
        
        opciones = state.slots_json.get("opciones_evento")

        if opciones:
            state.slots_json["awaiting"] = "event_selection"
            await session.commit()

            lines = []
            for i, ev in enumerate(opciones, 1):
                titulo = ev.get("summary") or "(sin título)"
                inicio = ev.get("start") or "—"
                fin = ev.get("end") or "—"
                lines.append(f"{i}. {titulo} — {inicio} a {fin}")

            await send_text(
                to_phone=phone,
                body="Elegí el evento (respondé con el número):\n" + "\n".join(lines)
            )
            return Response(status_code=200)

        if faltantes:
            await send_text(
                to_phone=phone,
                body=(f"Me faltan: *{', '.join(faltantes)}*.\n" + "Decime lo que falta o confirmá para continuar.")
            )
            return Response(status_code=200)

        # Sin opciones y sin faltantes → ejecutar y responder
        resultado, resumen = await ejecutar_accion_calendar(
            intent=state.intent_actual,
            slots=state.slots_json,
            calendar_client=calendar_client,
            timezone="America/Argentina/Buenos_Aires",
        )

        # Reset total del estado tras ejecutar con éxito
        state.intent_actual = None
        state.slots_json = {}
        state.pending_intent = None
        state.pending_message = None
        await session.commit()

        await send_text(to_phone=phone, body=resumen)
        return Response(status_code=200)
    
    
     