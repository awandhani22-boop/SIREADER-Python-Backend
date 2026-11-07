import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from dotenv import dotenv_values

config = dotenv_values(".env")

api_key = config.get("AI_API_KEY")
base_url = config.get(
    "AI_BASE_URL",
    "http://127.0.0.1:20128/v1"
).rstrip("/")

if not api_key or api_key == "PASTE_API_KEY_OMNIROUTE_DI_SINI":
    print("API KEY BELUM DIISI.")
    raise SystemExit(1)

request = Request(
    f"{base_url}/models",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    },
)

try:
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    models = data.get("data", [])

    print("STATUS: BERHASIL")
    print("HTTP: 200")
    print("JUMLAH MODEL:", len(models))
    print("\nDAFTAR MODEL:")

    for model in models:
        print("-", model.get("id", "Unknown"))

except HTTPError as error:
    print("HTTP ERROR:", error.code)
    print("Respons:", error.read().decode("utf-8", errors="replace")[:500])

except URLError as error:
    print("KONEKSI GAGAL:", error.reason)

except Exception as error:
    print("ERROR:", type(error).__name__, str(error))
