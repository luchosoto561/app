"""
database configuration layer.
"""
#sirve para poder poner como type hint una clase que todavia no fue definida 
from __future__ import annotations
#forma de indicar en anotaciones de tipo que una funcion async devuelve un generador (funcion que en vez de usar return usa yield -> cada vez que llamas a la funcion te va dando un valor y se queda esperando) en este caso sesiones de base de datos
from typing import AsyncGenerator

#part asincronica de SQLAlchemy, nos da una sesion Asyncronica para hablar con la base, forma de crear un motor de conexiona Postgres usando asyncpg y una fabrica de sesiones, para no tener que instanciarlas a mano todo el tiempo
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from core.config import settings

#el engine es como el motor de conexion a la base de datos, no ejecuta queries directamente, lo que hace es manejar la conexion entre tu app y la Base de Datos, tus Session usan este engine por debajo para hablar con la base de datos
engine = create_async_engine(
    settings.DATABASE_URL,  
    echo=False,             
    pool_pre_ping=True,     
)


# 3) Session factory -> cada request usa su propia sesión
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,  # evita que tengas que refrescar objetos después de commit
)


# 4)es una funcion de dependencia que proporciona una sesion por request y se encarga de cerrarla al final
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Usala en tus endpoints:
        async def endpoint(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            # el context manager ya cierra, esto es por claridad
            await session.close() 