#es la parte Object Relational Mapper y nos da una clase que sera la base de la que heredan todos los modelos, esto le dice a SQLAlchemy, estas clases representan tablas. Piensalo asi, el ORM traduce clases de Python a tablas de SQL
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
