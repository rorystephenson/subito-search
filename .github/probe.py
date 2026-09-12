"""Which requests, if any, get through to subito from a GitHub runner?"""
import json, requests

URL = "https://hades.subito.it/v1/search/items"
PARAMS = {"q": "bici", "lim": 3, "sort": "datedesc"}
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

print("runner egress IP:", requests.get("https://api.ipify.org", timeout=15).text)
try:
    info = requests.get("https://ipinfo.io/json", timeout=15).json()
    print("  org:", info.get("org"), "| region:", info.get("region"), info.get("country"))
except Exception as e:
    print("  ipinfo failed:", e)

def probe(label, headers=None, session=None, url=URL, params=PARAMS):
    s = session or requests.Session()
    try:
        r = s.get(url, params=params, headers=headers or {}, timeout=20)
        body = r.text[:110].replace("\n", " ")
        print(f"  {r.status_code}  {label:38} {body}")
        return r.status_code
    except Exception as e:
        print(f"  ERR {label:38} {e}")
        return None

print("\n--- header variants ---")
probe("no headers")
probe("current bot headers", {
    "User-Agent": CHROME, "Accept": "application/json",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://www.subito.it/", "Origin": "https://www.subito.it"})
probe("full chrome fingerprint", {
    "User-Agent": CHROME, "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.subito.it/", "Origin": "https://www.subito.it",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive", "DNT": "1"})

print("\n--- warm session (fetch homepage first for cookies) ---")
s = requests.Session()
s.headers.update({"User-Agent": CHROME, "Accept-Language": "it-IT,it;q=0.9"})
try:
    home = s.get("https://www.subito.it/", timeout=25)
    print(f"  homepage -> {home.status_code}, cookies: {list(s.cookies.keys())}")
except Exception as e:
    print("  homepage failed:", e)
probe("after homepage", {"Accept": "application/json", "Referer": "https://www.subito.it/"}, session=s)

print("\n--- other hosts ---")
probe("apparent www host", {"User-Agent": CHROME, "Accept": "application/json"},
      url="https://www.subito.it/hades/v1/search/items")
