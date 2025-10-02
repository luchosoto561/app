from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session
from core.google_oauth import build_state, build_auth_url, exchange_code_for_tokens, parse_State
from services.google_auth_store import upsert_google_tokens

router = APIRouter(prefix="/auth/google", tags=["Google OAuth"])

@router.get("/start")
async def start(phone: str = Query(..., min_length=5, description="Teléfono WhatsApp en formato E.164 o similar"), force_consent: bool = Query(False), select_account: bool = Query(False)):
    """
    Recibe un teléfono y redirige a la pantalla de consentimiento de Google.
    Usalo en el navegador para probar manualmente o para generar el link que le mandás por WhatsApp.
    """
    
    
    # payload mínimo que necesitaremos recuperar en el callback
    state = build_state({"phone": phone})
    auth_url = build_auth_url(state, force_consent=force_consent, select_account=select_account)
    
    print(auth_url, flush=True)
    # Para pruebas manuales, redirigimos; si querés, podrías devolver JSON con {"auth_url": "..."}
    
    return RedirectResponse(auth_url, status_code=307)

@router.get("/callback")
async def callback(state: str | None = None, code: str | None = None, session: AsyncSession = Depends(get_session)):
    """
    Callback de Google: valida state, canjea code por tokens y los guarda.
    Devuelve confirmación simple (JSON). En producción podés redirigir a una página de éxito.
    """
    if not state or not code:
        raise HTTPException(status_code=400, detail="Missing 'state' or 'code'")

    # TTL razonable (ej. 10 minutos). Si querés, hacelo configurable.
    data = parse_State(state, max_age_seconds=600)
    phone = data.get("phone") if isinstance(data, dict) else None
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    # Intercambiamos code por tokens
    token_data = await exchange_code_for_tokens(code)
    # Guardamos/actualizamos sin pisar refresh_token si viene None (lo maneja tu store)
    modelo = await upsert_google_tokens(session, phone=phone, token_data=token_data)
    
    # (Opcional) Aquí podrías disparar un ping a Calendar (freeBusy) para certificar conectividad.
    return JSONResponse(
        {
            "status": "ok",
            "message": "Google Calendar conectado correctamente.",
            "phone": phone,
            "email": modelo.email,
            "sub": modelo.google_sub
        },
        status_code=200,
    )