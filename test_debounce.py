
import asyncio
from threading import Event

from app.ingestion.debounce import MessageDebouncer



def test_pesan_berdekatan_digabung():
    processed = []
    completed = Event()

    def fake_processor(chat_id, message_id):
        processed.append((chat_id, message_id))
        completed.set()
        return {"status": "ANALYZED"}

    async def scenario():
        debouncer = MessageDebouncer(
            delay_seconds=0.05,
            processor=fake_processor,
        )

        try:
            debouncer.schedule(-100, 101)

            await asyncio.sleep(0.02)

            debouncer.schedule(-100, 102)

            await asyncio.sleep(0.02)

            debouncer.schedule(-100, 103)

            # Tunggu sampai processor benar-benar selesai.
            finished = await asyncio.to_thread(
                completed.wait,
                2,
            )

            assert finished, "Processor tidak selesai dalam 2 detik."

            assert processed == [(-100, 103)]

        finally:
            await debouncer.close()

    asyncio.run(scenario())

def test_chat_berbeda_diproses_terpisah():
    processed = []

    def fake_processor(chat_id, message_id):
        processed.append((chat_id, message_id))
        return {"status": "ANALYZED"}

    async def scenario():
        debouncer = MessageDebouncer(
            delay_seconds=0.05,
            processor=fake_processor,
        )

        try:
            debouncer.schedule(-100, 101)
            debouncer.schedule(-200, 201)

            await asyncio.sleep(0.10)

            assert set(processed) == {
                (-100, 101),
                (-200, 201),
            }

        finally:
            await debouncer.close()

    asyncio.run(scenario())


def test_pesan_baru_saat_ai_bekerja_tidak_hilang():
    processed = []

    started = Event()
    release = Event()
    second_done = Event()

    def fake_processor(chat_id, message_id):
        processed.append(message_id)

        if message_id == 101:
            started.set()

            # Simulasikan AI yang sedang bekerja.
            release.wait(timeout=2)

        if message_id == 102:
            second_done.set()

        return {"status": "ANALYZED"}

    async def scenario():
        debouncer = MessageDebouncer(
            delay_seconds=0.02,
            processor=fake_processor,
        )

        try:
            debouncer.schedule(-100, 101)

            # Tunggu sampai pemrosesan pertama dimulai.
            started_ok = await asyncio.to_thread(
                started.wait,
                1,
            )

            assert started_ok

            # Pesan baru masuk saat AI pertama bekerja.
            debouncer.schedule(-100, 102)

            # Lepaskan simulasi AI pertama.
            release.set()

            # Tunggu pemrosesan pesan kedua selesai.
            second_ok = await asyncio.to_thread(
                second_done.wait,
                2,
            )

            assert second_ok
            assert processed == [101, 102]

        finally:
            release.set()
            await debouncer.close()

    asyncio.run(scenario())
