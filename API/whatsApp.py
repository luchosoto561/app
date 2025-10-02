"""comunicacion HTTP con whatsApp"""
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

    # 2c) Si está todo OK (token vigente o recién refrescado), seguí con la intención del usuario
    # todo: enrutar por intención (crear / consultar / actualizar / eliminar evento)
    # Por ahora, solo respondemos que está todo listo.
    await send_text(
        to_phone=phone,
        body="¡Listo! Ya tengo acceso a tu calendario. ¿Qué querés hacer? (crear/consultar/actualizar/eliminar)",
    )
    return Response(status_code=200)