"""aca es donde tengo que crear la app de FastAPI y es basicamente el encendido de la aplicacion"""

from fastapi import FastAPI
from API import whatsApp
from API import auth_google


app = FastAPI()

app.include_router(whatsApp.router)
app.include_router(auth_google.router)

@app.get("/hello")
async def saludo():
    return "hello world"