"""
es un wrapper (capa de servicio) que encapsula como le envias un mensaje de texto a un usuario por Whatsapp Coud API (Meta). Tu webhook decide cuando y que mandar, este
servicio se encarga de mandarlo bien (URL, headers, formato JSON, token, timeouts, manejo de error)

ventajas de tenerlo separado:
- Aisla configuracion sensible (token, phone number id).
- Centraliza la llamada HTTP (si Meta cambia version, lo tocas aca)
- Hace robusto el envio: timeout, raise_for_status, captura errores, sin romper el 200 ok del webhook

"""
import re
import httpx
from core.config import settings

#url para enviar mensajes en whatsapp, en whatsapp api cloud entras en referencias, mensajes y te indica que le podes mandar en el payload que es json y en los headers
API_URL = (f"{settings.GRAPH_API_BASE.rstrip('/')}/"f"{settings.GRAPH_API_VERSION.strip('/')}/"f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages")

async def send_text(*, to_phone: str, body: str) -> None:
    """
    Envía un mensaje de texto por WhatsApp Cloud API.
    Best-effort: loguea error pero no levanta excepción (para no romper el 200 del webhook).
    """
    #si no se tiene el token o el id, no enviamos el mensaje
    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        print("FALTAN VARS: WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID", flush=True)
        return {"ok":False}
    
    to_phone_secure = to_541115(to_phone)
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_secure,
        "type": "text",
        "text": {"preview_url": True, "body": body[:4096]},
    }

    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(API_URL, json=payload, headers=headers)
            print("STATUS=", resp.status_code, flush=True)
            print("RESP=", resp.text, flush=True)
            return {"ok": resp.status_code < 400, "status": resp.status_code, "resp": resp.text}
    except Exception:
        # En prod: loguear con stacktrace y request_id de Meta si está disponible
        return 


def to_541115(number: str) -> str:
    """
    Si recibe un número en formato +54911XXXXXXXX (móvil AMBA),
    lo transforma a +541115XXXXXXXX.
    En cualquier otro caso, devuelve el número normalizado con '+'.

    Ej:
      +5491130643879 -> +54111530643879
    """
    # quitar todo lo que no sea dígito
    digits = re.sub(r"\D", "", number)

    # caso objetivo: 54 9 11 XXXXXXXX
    if digits.startswith("54911"):
        rest = digits[5:]  # quitar '54911'
        return f"+541115{rest}"

    # en otros casos, solo devolver normalizado con '+'
    return f"+{digits}"
