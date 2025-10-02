"""
Implementa la lógica OAuth con Google (sin persistencia)
"""

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from core.config import settings
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
import httpx
from core.config import settings


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth" #direccion del servidor de autorizacion de google, a ese servidor va el usuario para ver la pantalla de concentimiento
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


_serializer = URLSafeTimedSerializer(
    secret_key=settings.SECRET_KEY,
    salt="google-oauth-state"#etiqueta extra para aislar usos
    
)
"""
crea un state seguro a partir de payload (dict), este estado es usado para armar la url de concentimiento hacia google,
pyload es un diccionario minimo que tiene la info que queres recuperar en el callback. 
"""
def build_state(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    return _serializer.dumps(payload)


"""
recibe un state y si fue modificado a lo largo del camino tira error
"""
def parse_State(state: str, max_age_seconds: int = 600) -> dict:   
    if not state:
        return {}
    try:
        data = _serializer.loads(state, max_age=max_age_seconds)
        return data if isinstance(data, dict) else {}
    except SignatureExpired:#salta si el ticket vencio
        return {}
    except BadSignature:#salta si alguien modifico el contenido
        return {}
    


def build_auth_url(state: str, *, force_consent: bool = False, select_account: bool = False) -> str:
    """
    retorna URL completa para redirigir al usuario (o enviarle por WhatsApp).

    """
    if not isinstance(state, str) or not state:
        raise ValueError("state must be a non-empty string")

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,               # Identifica TU app ante Google
        "redirect_uri": str(settings.GOOGLE_REDIRECT_URI),    # Debe coincidir EXACTO con lo configurado en Google Cloud
        "response_type": "code",                               # Pedimos un authorization code (paso previo al token)
        "scope": " ".join(settings.GOOGLE_SCOPES),             # Permisos; separados por espacio
        "access_type": "offline",                              # Para obtener refresh_token (sesión larga)
        "include_granted_scopes": "true",                      # Incremental auth (no vuelve a pedir lo ya aceptado)
        "state": state,                                        # Tu “papelito” firmado (anti-CSRF + contexto)
    }

    prompts: list[str] =[]
    if force_consent:
        prompts.append("consent") # fuerza a mostrar concentimiento, util para refresh roto
    if select_account:
        prompts.append("select_account") # fuerza selector de cuentas
    
    if prompts:
        # google permite varios prompts separados por espacio
        params["prompt"] = " ".join(prompts)

    return f"{AUTH_ENDPOINT}?{urlencode(params)}"



async def exchange_code_for_tokens(code: str) -> dict:
   
    if not code or not isinstance(code, str):
        raise ValueError("code must be a non-empty string")

    # Lo que Google espera recibir para canjear el "code" por tokens
    form = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": str(settings.GOOGLE_REDIRECT_URI),  # debe coincidir EXACTO con la consola
        "grant_type": "authorization_code",
    }

    # POST servidor→servidor (tu client_secret NUNCA viaja al navegador)
    """
    AsyncClient es una clase de httpx para hacer requests asincronicas (compatibles con async def y await), en tu api si tu endpoint es async def, usar AsyncClient es lo natural. Si tu codigo
    fuera sincrono, usarias httpx.Client.
    timeout=15 le dice al cliente "no esperes mas de 15 segundos por la request, asi evitamos que el servidor quede colgado si el remoto no responde"
    """
    async with httpx.AsyncClient(timeout=15) as client:
        #se hace el post
        resp = await client.post(
            TOKEN_ENDPOINT,
            data=form,  # x-www-form-urlencoded por defecto
            headers={"Accept": "application/json"},
        )
        #si google responde on 4xx o 5xx, raise_for_status() lanza HTTPStatusError
        resp.raise_for_status()
        raw = resp.json()#convierte el cuerpo JSON de la respuesta en un dict de Python

    # Normalizamos y calculamos cuándo expira el access_token
    expires_in = raw.get("expires_in", 0) or 0 #si pasa algo raro lo tratamos como que ya vencio, por seguridad
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in) #lo transformamos a hora porque es mas comodo

    return {
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token"),  # puede venir solo la 1ª vez
        "token_type": raw.get("token_type", "Bearer"),
        "scope": raw.get("scope"),
        "expires_at": expires_at, # listo para guardar como timestamp/DateTime UTC
        
        # traigo el sub (id de la cuenta de google) y el gmail. Estos datos son de la cuenta que dio acceso
        "id_token": raw.get("id_token") 
    }
    
    

async def refresh_access_token(refresh_token: str) -> dict:
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("refresh_token must be a non-empty string")

    form = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data=form,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        raw = resp.json()

    expires_in = raw.get("expires_in", 0) or 0
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    return {
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token"),
        "token_type": raw.get("token_type", "Bearer"),
        "scope": raw.get("scope"),
        "expires_at": expires_at,
    }