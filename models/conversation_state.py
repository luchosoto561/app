# models/conversation_state.py
from __future__ import annotations

from sqlalchemy import String, Text, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class ConversationState(Base):
    """
    Estado mínimo por usuario para manejar intención y slots.
    Una sola fila por whatsapp_phone (intención activa o None).
    """
    __tablename__ = "conversation_state"

    # Clave primaria interna por conveniencia
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Relación 1–1 con credenciales del usuario (mismo número, formato E.164)
    whatsapp_phone: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("google_credentials.whatsapp_phone", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # una fila por usuario
    )

    # Intención actual (None => sin tarea en curso)
    intent_actual: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Slots acumulados/pendientes para la intención actual (JSONB en Postgres)
    slots_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Cambio de intención pendiente (si el usuario lo pidió y está por confirmarse)
    pending_intent: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Mensaje que disparó el posible cambio (para reinyectarlo si confirma)
    pending_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ConversationState phone={self.whatsapp_phone!r} intent={self.intent_actual!r}>"
