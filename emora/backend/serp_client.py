"""
Thin wrapper around SerpApi's Google Search endpoint.
Requires SERPAPI_KEY in the environment (see .env.example).

Results are cached in memory per query so repeated lessons don't burn
your free-tier search quota. Error messages never include the request URL,
because that URL contains your API key.
"""
import os

import requests

SERPAPI_URL = "https://serpapi.com/search.json"
_CACHE: dict = {}


def search(query: str, num_results: int = 5) -> list[dict]:
    key = (query.strip().lower(), num_results)
    if key in _CACHE:
        return _CACHE[key]

    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_KEY is not set (add it to backend/.env).")

    try:
        resp = requests.get(
            SERPAPI_URL,
            params={"engine": "google", "q": query, "api_key": api_key, "num": num_results},
            timeout=15,
        )
    except requests.RequestException:
        raise RuntimeError("Could not reach SerpApi.") from None

    if resp.status_code >= 400:
        raise RuntimeError(f"SerpApi returned HTTP {resp.status_code}.")

    data = resp.json()
    if data.get("error") and not data.get("organic_results"):
        if "hasn't returned any results" in data["error"]:
            return []
        raise RuntimeError("SerpApi: " + str(data["error"])[:150])

    results = [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": item.get("source", ""),
        }
        for item in data.get("organic_results", [])[:num_results]
    ]
    _CACHE[key] = results
    return results
