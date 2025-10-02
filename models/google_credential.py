
from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Text, DateTime, func, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base  # asumimos que tu db.py expone Base (Declarative Base)

"""
guarda toda la informacion necesaria para operar en nombre del usuario con Google Calendar
"""
class GoogleCredential(Base):
    __tablename__ = "google_credentials"

    # Clave primaria interna por conveniencia
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Identificador de tu usuario (ej. número de WhatsApp en formato E.164 "54911...")
    whatsapp_phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

     # identidad de la cuenta Google vinculada
    google_sub: Mapped[str | None] = mapped_column(String(64), nullable=True)   # en V1 podemos arrancar nullable y completar a medida que la gente re-consiente
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    
    # Tokens y metadatos de OAuth
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(20), nullable=False, default="Bearer")
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Vencimiento del access_token (SIEMPRE en UTC)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Timestamps de auditoría
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = ()

    def __repr__(self) -> str:
        return f"<GoogleCredential phone={self.whatsapp_phone!r}>"