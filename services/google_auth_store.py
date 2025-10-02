# services/google_auth_store.py
from __future__ import annotations

import base64
import json

from datetime import datetime
from typing import Optional, Dict, Any, Tuple, TypedDict, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.google_credential import GoogleCredential

from datetime import datetime, timezone, timedelta

import httpx
from core.google_oauth import refresh_access_token
from models.google_credential import GoogleCredential

async def get_google_credentials(session: AsyncSession, *, phone: str) -> Optional[GoogleCredential]:
    """
    Devuelve las credenciales guardadas para este 'phone', o None si no existen.
    """
    stmt = select(GoogleCredential).where(GoogleCredential.whatsapp_phone == phone)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()



def _decode_id_token_unverified(id_token: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Decodifica el payload del id_token (JWT) sin validar firma (suficiente para V1),
    y devuelve (sub, email). Si no puede decodificar, devuelve (None, None).

    NOTA: esto NO verifica criptográficamente el token; en V2 podés validar firma con JWKS.
    """
    if not id_token or not isinstance(id_token, str):
        return None, None
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return None, None
        # Base64URL decode del payload (parte 1)
        payload_b64 = parts[1]
        """padding contiene la cantidad de igules que se le tiene que sumar a payload_64 para que la cantidad de caracteres sea multiplo de 4,
        esto es porque de tal manera lo prefiere Python, luego se codifica a bytes y se decodifica a json, para por fin lograr el unico objetivo de esto
        que es tener en payload el json con sub, email"""
        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8"))
        payload = json.loads(payload_json.decode("utf-8"))

        sub = payload.get("sub")
        email = payload.get("email")
        return (sub if isinstance(sub, str) else None,
                email if isinstance(email, str) else None)
    except Exception:
        return None, None


async def upsert_google_tokens(session: AsyncSession, *, phone: str, token_data: Dict[str, Any]) -> GoogleCredential:
    """
    Inserta o actualiza credenciales para 'phone'.

    Contrato esperado en token_data:
      - access_token: str (OBLIGATORIO)
      - expires_at: datetime (UTC) (OBLIGATORIO, calculado a partir de 'expires_in')
      - refresh_token: Optional[str] (puede faltar; Google no siempre lo manda)
      - token_type: Optional[str] (default 'Bearer' si falta)
      - scope: Optional[str]        (puede faltar en refresh)
      - id_token: Optional[str]     (si pediste 'openid'; trae identidad 'sub' y 'email')
    """
    access_token: Optional[str] = token_data.get("access_token")
    expires_at: Optional[datetime] = token_data.get("expires_at")
    if not access_token:
        raise ValueError("token_data['access_token'] es obligatorio")
    if not isinstance(expires_at, datetime):
        raise ValueError("token_data['expires_at'] (datetime UTC) es obligatorio")

    refresh_token: Optional[str] = token_data.get("refresh_token")
    token_type: str = token_data.get("token_type") or "Bearer"
    scope: Optional[str] = token_data.get("scope")

    # Identidad (si pediste 'openid'): google_sub y email
    id_token: Optional[str] = token_data.get("id_token")
    google_sub, email = _decode_id_token_unverified(id_token)

    # ¿Existe ya una fila para este phone?
    cred = await get_google_credentials(session, phone=phone)

    if cred is None:
        # Insert
        cred = GoogleCredential(
            whatsapp_phone=phone,
            google_sub=google_sub,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,  # puede ser None si Google no envió uno
            token_type=token_type,
            scope=scope,
            expires_at=expires_at,
        )
        session.add(cred)
    else:
        # Update idempotente
        if google_sub:
            # Si el usuario re-consintió con otra cuenta, sobreescribimos la identidad (V1 = 1 cuenta por teléfono)
            cred.google_sub = google_sub
        if email:
            cred.email = email

        cred.access_token = access_token
        cred.token_type = token_type or cred.token_type
        cred.scope = scope or cred.scope
        cred.expires_at = expires_at or cred.expires_at

        # refresh_token SOLO si vino uno nuevo (Google puede rotarlo; en refresh normalmente no llega)
        if refresh_token:
            cred.refresh_token = refresh_token

    await session.commit()
    return cred



"""esta funcion recibe una hora a la que expira el token y lo marca como expirado un tiempo antes,
que es esa hora menos el margen, porque el margen es el tiempo que se le da al token para que sea utilizado"""
def is_access_valid(expires_at: datetime, margin_seconds: int = 300) -> bool:
    """Devuelve True si el access_token sigue vigente considerando un margen de seguridad."""
    if not isinstance(expires_at, datetime):
        return False
    now = datetime.now(timezone.utc)
    return now < (expires_at - timedelta(seconds=margin_seconds))


class RefreshResult:
    OK = "ok"
    INVALID_GRANT = "invalid_grant"        # refresh roto → re-consent
    TRANSIENT_ERROR = "transient_error"    # red/5xx/timeout → reintentar
    CONFIG_ERROR = "config_error"          # payload/client mal (no sirve re-consent)
    

async def try_refresh(session: AsyncSession, cred: GoogleCredential) -> str:
    """
    Intenta refrescar el access_token usando el refresh_token guardado.
    Si sale bien, actualiza la DB (vía upsert_google_tokens) y devuelve OK.
    Si falla, clasifica el motivo.
    """
    if not cred.refresh_token:
        # No hay nada para refrescar: tratamos como inválido (pedir re-consent)
        return RefreshResult.INVALID_GRANT

    try:
        # Pide nuevos tokens a Google
        token_data = await refresh_access_token(cred.refresh_token)

        # Si Google no envía refresh_token nuevo, conservamos el actual
        if not token_data.get("refresh_token"):
            token_data["refresh_token"] = cred.refresh_token

        # Guardamos los nuevos valores
        await upsert_google_tokens(session, phone=cred.whatsapp_phone, token_data=token_data,)
        return RefreshResult.OK

    except httpx.HTTPStatusError as e:
        # Google respondió con 4xx/5xx
        err_code = None
        try:
            body = e.response.json()
            err_code = body.get("error")
        except Exception:
            pass

        if e.response.status_code == 400 and err_code == "invalid_grant":
            return RefreshResult.INVALID_GRANT

        if e.response.status_code >= 500:
            return RefreshResult.TRANSIENT_ERROR

        return RefreshResult.CONFIG_ERROR

    except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError):
        # Errores de red / timeouts → transitorio
        return RefreshResult.TRANSIENT_ERROR
    



class EnsureDecision(TypedDict, total=False):
    status: Literal["ok", "link_sent", "need_reconsent", "need_refresh", "no_credentials"]
    action: Literal["none", "send_link"]
    reason: str
    prompts: dict               # {"force_consent": bool, "select_account": bool}
    cred: Optional[GoogleCredential]
    


    
async def ensure_access(session: AsyncSession, *, phone: str) -> EnsureDecision:
    cred = await get_google_credentials(session, phone=phone)

    if cred is None:
        return {
            "status": "no_credentials",
            "action": "send_link",
            "reason": "no_credentials",
            "prompts": {"force_consent": False, "select_account": False},
            "cred": None,
        }

    # margen generoso (p. ej. 300s)
    if is_access_valid(cred.expires_at, margin_seconds=300):
        return {
            "status": "ok",
            "action": "none",
            "reason": "access_valid",
            "cred": cred,
        }

    # vencido o por vencer → intentamos refrescar
    outcome = await try_refresh(session, cred)

    if outcome == RefreshResult.OK:
        # opcional: recargar credenciales actualizadas
        cred = await get_google_credentials(session, phone=phone)
        return {
            "status": "ok",
            "action": "none",
            "reason": "refreshed",
            "cred": cred,
        }

    if outcome == RefreshResult.INVALID_GRANT:
        return {
            "status": "need_reconsent",
            "action": "send_link",
            "reason": "refresh_invalid_grant",
            "prompts": {"force_consent": True, "select_account": False},
            "cred": cred,
        }

    if outcome == RefreshResult.TRANSIENT_ERROR:
        return {
            "status": "need_refresh",
            "action": "none",
            "reason": "refresh_transient_error",
            "cred": cred,
        }

    # CONFIG_ERROR u otro
    return {
        "status": "need_refresh",
        "action": "none",
        "reason": "config_error",
        "cred": cred,
    }    