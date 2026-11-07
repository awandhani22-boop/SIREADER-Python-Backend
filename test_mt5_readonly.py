import MetaTrader5 as mt5

MT5_PATH = r"C:\Program Files\HFM Metatrader 5\terminal64.exe"


def main():
    try:
        print("Menghubungkan ke HFM MT5...")

        if not mt5.initialize(
            path=MT5_PATH,
            timeout=60000,
        ):
            print("KONEKSI GAGAL:", mt5.last_error())
            return

        print("Koneksi Python-MT5 berhasil.")

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None or account is None:
            print("Informasi terminal atau akun tidak tersedia.")
            print("Error:", mt5.last_error())
            return

        modes = {
            mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
            mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
            mt5.ACCOUNT_TRADE_MODE_REAL: "REAL",
        }

        mode = modes.get(account.trade_mode, "UNKNOWN")

        print("\n===== TERMINAL =====")
        print("Terminal:", terminal.name)
        print("Company:", terminal.company)
        print("Connected:", terminal.connected)
        print("Trade allowed:", terminal.trade_allowed)
        print("Trade API disabled:", terminal.tradeapi_disabled)
        print("Path:", terminal.path)

        print("\n===== ACCOUNT =====")
        print("Account mode:", mode)
        print("Server:", account.server)
        print("Company:", account.company)
        print("Currency:", account.currency)
        print("Leverage:", account.leverage)

        # Jangan lanjutkan pemeriksaan trading jika bukan akun demo.
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            print("\nDIBATALKAN: gunakan akun DEMO untuk pengujian.")
            return

        if not terminal.connected:
            print("\nDIBATALKAN: terminal belum terhubung.")
            return

        print("\n===== XAU SYMBOLS =====")

        symbols = mt5.symbols_get()

        if symbols is None:
            print("Gagal membaca simbol:", mt5.last_error())
            return

        candidates = [
            symbol for symbol in symbols
            if "XAU" in symbol.name.upper()
        ]

        if not candidates:
            print("Simbol XAU tidak ditemukan.")
            return

        for symbol in candidates:
            info = mt5.symbol_info(symbol.name)

            if info is None:
                continue

            tick = mt5.symbol_info_tick(symbol.name)

            print("\n-----------------------")
            print("Symbol:", info.name)
            print("Visible:", info.visible)
            print("Digits:", info.digits)
            print("Point:", info.point)
            print("Bid:", tick.bid if tick else info.bid)
            print("Ask:", tick.ask if tick else info.ask)
            print("Tick size:", info.trade_tick_size)
            print("Tick value:", info.trade_tick_value)
            print("Contract size:", info.trade_contract_size)
            print("Volume min:", info.volume_min)
            print("Volume max:", info.volume_max)
            print("Volume step:", info.volume_step)
            print("Stops level:", info.trade_stops_level)
            print("Freeze level:", info.trade_freeze_level)
            print("Filling mode:", info.filling_mode)
            print("Trade mode:", info.trade_mode)

        print("\n===== READ-ONLY TEST SELESAI =====")
        print("Tidak ada order yang dikirim.")

    finally:
        mt5.shutdown()
        print("\nKoneksi Python ke MT5 ditutup.")


if __name__ == "__main__":
    main()
