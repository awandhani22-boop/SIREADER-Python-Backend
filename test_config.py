from app.config import settings

print("TRADING MODE:", settings.TRADING_MODE)
print("MANUAL APPROVAL:", settings.REQUIRE_MANUAL_APPROVAL)
print("TIMEZONE:", settings.TIMEZONE)
print("DATABASE:", "TERHUBUNG" if settings.DATABASE_URL else "KOSONG")