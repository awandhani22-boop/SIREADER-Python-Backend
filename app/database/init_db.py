from app.database.connection import engine
from app.database.models import Base
# Register Phase 4 generic workforce metadata before create_all.
import app.workers.models  # noqa: F401

Base.metadata.create_all(bind=engine)

print("TABEL DATABASE BERHASIL DIBUAT!")
