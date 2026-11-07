from pathlib import Path

from app.database.connection import SessionLocal
from app.database.models import TelegramMedia


def main():
    with SessionLocal() as db:
        media_list = db.query(TelegramMedia).all()

    total = len(media_list)
    valid = 0
    missing = []
    size_mismatch = []

    print("\n=== SIREADER MEDIA INTEGRITY TEST ===\n")

    for media in media_list:
        path = Path(media.file_path)

        if not path.exists():
            missing.append(media)
            print(f"[MISSING] {media.original_filename}")
            continue

        actual_size = path.stat().st_size

        if actual_size != media.file_size:
            size_mismatch.append(media)
            print(
                f"[SIZE MISMATCH] {media.original_filename} | "
                f"DB: {media.file_size} | File: {actual_size}"
            )
            continue

        valid += 1
        print(f"[OK] {media.original_filename}")

    print("\n=== HASIL PEMERIKSAAN ===")
    print(f"Total media       : {total}")
    print(f"File valid        : {valid}")
    print(f"File hilang       : {len(missing)}")
    print(f"Ukuran tidak cocok: {len(size_mismatch)}")

    if not missing and not size_mismatch:
        print("\nSEMUA MEDIA VALID!")
    else:
        print("\nADA MEDIA YANG PERLU DIPERIKSA!")


if __name__ == "__main__":
    main()