from app.database.connection import engine
from app.database.models import Base

Base.metadata.create_all(bind=engine)

print("TABEL DATABASE BERHASIL DIBUAT!")
