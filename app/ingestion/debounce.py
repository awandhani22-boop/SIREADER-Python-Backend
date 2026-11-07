
import asyncio


class MessageDebouncer:
    """
    Menggabungkan pesan berdasarkan chat.

    Setiap pesan baru mereset waktu tunggu.
    Pesan yang datang ketika proses AI berlangsung
    akan dijadwalkan sebagai batch berikutnya.
    """

    def __init__(
        self,
        delay_seconds,
        processor,
        on_result=None,
    ):
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, (int, float))
            or delay_seconds <= 0
        ):
            raise ValueError(
                "delay_seconds harus lebih besar dari nol."
            )

        if not callable(processor):
            raise TypeError("processor harus berupa fungsi.")

        self.delay_seconds = float(delay_seconds)
        self.processor = processor
        self.on_result = on_result

        self._states = {}
        self._closed = False

    def schedule(self, chat_id, message_id):
        """Jadwalkan pesan terbaru untuk diproses."""

        if self._closed:
            raise RuntimeError("Debouncer sudah ditutup.")

        state = self._states.get(chat_id)

        if state is None:
            state = {
                "generation": 0,
                "message_id": None,
                "task": None,
            }
            self._states[chat_id] = state

        state["generation"] += 1
        state["message_id"] = message_id

        task = state["task"]

        if task is None or task.done():
            state["task"] = asyncio.create_task(
                self._run(chat_id, state)
            )

        return state["generation"]

    async def _run(self, chat_id, state):
        current_task = asyncio.current_task()

        try:
            while not self._closed:
                generation = state["generation"]

                await asyncio.sleep(
                    self.delay_seconds
                )

                # Ada pesan baru selama masa tunggu.
                if state["generation"] != generation:
                    continue

                target_message_id = state["message_id"]

                try:
                    result = await asyncio.to_thread(
                        self.processor,
                        chat_id,
                        target_message_id,
                    )

                except Exception as error:
                    print(
                        f"[PIPELINE ERROR] Chat: {chat_id} | "
                        f"Message: {target_message_id} | {error}"
                    )

                else:
                    if self.on_result:
                        try:
                            self.on_result(
                                chat_id,
                                target_message_id,
                                result,
                            )
                        except Exception as error:
                            print(
                                f"[PIPELINE CALLBACK ERROR] {error}"
                            )

                # Jika ada pesan baru saat AI bekerja,
                # loop kembali dan menunggu debounce baru.
                if state["generation"] == generation:
                    break

        except asyncio.CancelledError:
            raise

        finally:
            if (
                self._states.get(chat_id) is state
                and state["task"] is current_task
            ):
                self._states.pop(chat_id, None)

    async def close(self):
        """Batalkan task debounce saat listener dihentikan."""

        self._closed = True

        tasks = [
            state["task"]
            for state in self._states.values()
            if state["task"] is not None
        ]

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        self._states.clear()
