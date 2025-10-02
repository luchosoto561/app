"""
core/config.py
---------------
Centraliza la configuración de la aplicación (no del usuario).
- Lee GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI, GOOGLE_SCOPES, SECRET_KEY desde el entorno.
- Expone un objeto `settings` para que el resto del código no hardcodee credenciales ni URLs.
- No gestiona tokens de usuarios ni toca la base de datos.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl
from typing import List

class Settings(BaseSettings):
    #Google OAuth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: AnyHttpUrl
    #lista de permisos que le vas a pedir al usuario que te de, en este caso es para leer y escribir en su calendar. Luego google tambien nos dara un id_token que incluye sub y email. 
    GOOGLE_SCOPES: List[str] = ["https://www.googleapis.com/auth/calendar", "openid", "email"]
    #clave interna para firmar y validar 'state'
    SECRET_KEY: str
    #saca la conexion a la base de datos del .env
    DATABASE_URL: str
    
    WHATSAPP_PHONE_NUMBER_ID: str
    WHATSAPP_TOKEN: str
    GRAPH_API_BASE: str = "https://graph.facebook.com"
    GRAPH_API_VERSION: str = "v23.0"
    
    #le decis que cargue los datos de un .env que esta en la carpeta base del proyecto
    class Config:
        env_file = ".env" #usado solo en desarrollo

#objeto unico que usara toda la app

settings = Settings()

