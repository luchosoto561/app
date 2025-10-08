from __future__ import annotations

import os
import time
from urllib.parse import quote_plus
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

router = APIRouter()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "VERIFICATION_123")

# Anti-spam muy simple en memoria (MVP)
_LAST_LINK_SENT_AT: dict[str, float] = {}
LINK_COOLDOWN_SECONDS = 120  # no re-enviar link más de 1 vez cada 2 min por número


#verificacion inicial del webhook de whatsapp -> mostramos que somos nosotros
@router.get("/webhook")
async def verify(request: Request):
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
            # Confirmó el cambio: aplicamos nueva intención y reinyectamos el mensaje que la disparó
            state.intent_actual = state.pending_intent
            state.slots_json = {}  # al cambiar de intención, reseteamos slots
            state.pending_intent = None
            await session.commit()

            # Avisamos y pedimos datos (los slots se implementan después)
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
        state.slots_json = {}
        await session.commit()
        await send_text(
            to_phone=phone,
            body=f"Listo, vamos con *{detected.replace('_', ' ').title()}*. Contame los detalles."
        )
        return Response(status_code=200)

    else:
        # Hay intención vigente
        if detected :
            # Propuesta de cambio: guardamos pendiente y pedimos confirmación
            state.pending_intent = detected
            state.pending_message = message_text
            await session.commit()
            await send_text(
                to_phone=phone,
                body=f"Estás en *{state.intent_actual.replace('_', ' ').title()}*. ¿Querés cambiar a *{detected.replace('_', ' ').title()}*? (sí/no)"
            )
            return Response(status_code=200)
        
        """
        aca tenemos una intencion clara por lo que ahora tenemos que llenar el json con los datos que pide calendar. 
        """
        # Sin cambio de intención: seguimos con la actual → próximamente: extracción de slots
        # Por ahora, pedimos datos de manera genérica hasta que conectes el slots_extractor
        await send_text(
            to_phone=phone,
            body=f"Continuemos con *{state.intent_actual.replace('_', ' ').title()}*. Decime fecha y hora (y lo que tengas) y lo proceso."
        )
        return Response(status_code=200)
    
    
     